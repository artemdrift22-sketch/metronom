"""Метроном · облачная база Turso без библиотек (только стандартный Python 3.9+).

Как работает: бот читает и пишет в обычную SQLite-копию в памяти — так же быстро, как с файлом.
Каждое изменение в фоне, пачками раз в пару секунд, уходит в Turso по HTTP (/v2/pipeline).
При запуске копия целиком загружается из Turso — поэтому перезапуск Render ничего не стирает.

Пачка уходит одной транзакцией с номером: если связь оборвалась после записи и пачку пришлось
отправить ещё раз, Turso узнает номер и второй раз её не применит.

connect("libsql://…" или "https://…", token) — облако; любой другой путь — обычный файл SQLite.
"""
import atexit
import base64
import json
import logging
import os
import re
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("tursodb")

FLUSH_EVERY = 2.0            # как часто отправлять изменения, сек
MAX_BATCH = 400              # операций в одной пачке (большие транзакции уходят целиком)
PAGE_ROWS = 1000             # строк за запрос при загрузке копии
WRITE_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", re.I)
TX_RE = re.compile(r"^\s*(BEGIN|COMMIT|END|ROLLBACK)\b", re.I)
KV_RE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+kv\s*\(\s*k\s*,\s*v\s*\)", re.I)
DUP = "metronom_dup"
SYNC_SQL = [
    "CREATE TABLE IF NOT EXISTS _sync(id INTEGER PRIMARY KEY, seq INTEGER NOT NULL)",
    "INSERT OR IGNORE INTO _sync(id, seq) VALUES(1, 0)",
    "CREATE TRIGGER IF NOT EXISTS _sync_once BEFORE UPDATE ON _sync WHEN NEW.seq <= OLD.seq "
    "BEGIN SELECT RAISE(ABORT, '" + DUP + "'); END",
]


class TursoError(Exception):
    def __init__(self, msg, http=0):
        super().__init__(msg)
        self.http = http


def is_remote(url):
    return bool(re.match(r"^(libsql|https?|wss?)://", str(url or "").strip(), re.I))


def http_url(url):
    url = str(url).strip()
    m = re.match(r"^(libsql|wss?|https?)://(.*)$", url, re.I)
    if m:
        scheme = m.group(1).lower()
        url = ("http://" if scheme in ("http", "ws") else "https://") + m.group(2)
    else:
        url = "https://" + url
    return url.split("?")[0].rstrip("/")


def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _val(c):
    t = (c or {}).get("type")
    if t in (None, "null"):
        return None
    if t == "integer":
        return int(c["value"])
    if t == "float":
        return float(c["value"])
    if t == "blob":
        s = c.get("base64") or c.get("value") or ""
        return base64.b64decode(s + "=" * (-len(s) % 4))
    return c.get("value")


def stmt(sql, args=()):
    s = {"sql": sql}
    if isinstance(args, dict):
        s["named_args"] = [{"name": k if k[:1] in ":@$" else ":" + k, "value": _arg(v)} for k, v in args.items()]
    elif args:
        s["args"] = [_arg(a) for a in args]
    return s


def split_sql(script):
    """Разбить скрипт на отдельные команды (учитывая кавычки и BEGIN…END в триггерах)."""
    out, buf = [], ""
    for part in script.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            s = buf.strip().rstrip(";").strip()
            if s:
                out.append(s)
            buf = ""
    if buf.strip().rstrip(";").strip():
        out.append(buf.strip().rstrip(";"))
    return out


# ───────────────────────── HTTP-клиент Turso ─────────────────────────

class Remote:
    def __init__(self, url, token="", timeout=25):
        self.url = http_url(url) + "/v2/pipeline"
        self.token = (token or "").strip()
        self.timeout = timeout

    def pipeline(self, requests):
        body = json.dumps({"baton": None, "requests": list(requests) + [{"type": "close"}]},
                          ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url, body, headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                text = ""
            raise TursoError(f"HTTP {e.code} {text}".strip(), e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TursoError(f"нет связи с Turso: {e}")
        res = data.get("results")
        if not isinstance(res, list) or len(res) < len(requests):
            raise TursoError("странный ответ Turso")
        return res[:len(requests)]

    @staticmethod
    def _result(r):
        if r.get("type") != "ok":
            raise TursoError(((r.get("error") or {}).get("message")) or "ошибка SQL")
        return r["response"]["result"]

    def execute(self, sql, args=()):
        return self._result(self.pipeline([{"type": "execute", "stmt": stmt(sql, args)}])[0])

    def rows(self, sql, args=()):
        res = self.execute(sql, args)
        cols = [c.get("name") for c in res.get("cols") or []]
        return cols, [[_val(v) for v in row] for row in res.get("rows") or []]

    def batch(self, steps):
        """steps: [(sql, args, condition|None)] → (step_results, step_errors)."""
        b = {"steps": [dict({"stmt": stmt(s, a)}, **({"condition": c} if c else {})) for s, a, c in steps]}
        r = self.pipeline([{"type": "batch", "batch": b}])[0]
        res = self._result(r)
        return res.get("step_results") or [], res.get("step_errors") or []


# ───────────────────────── соединение «как sqlite3» ─────────────────────────

class Connection:
    """Похоже на sqlite3.Connection: execute / executescript / commit / row_factory.
    Чтения — из локальной копии, изменения — сразу в копию и в фоне в Turso."""

    def __init__(self, url, token="", remote=None, flush_every=FLUSH_EVERY, local=":memory:", start=True, tries=6):
        self.remote = remote or Remote(url, token)
        self.local = sqlite3.connect(local, isolation_level=None, check_same_thread=False)
        self.flush_every = flush_every
        self._lock = threading.RLock()
        self._queue = []           # группы операций [(sql, args)], группа = одна транзакция
        self._tx = None            # операции текущей локальной транзакции
        self._inflight = None      # (номер, операции) — пачка, которая сейчас уходит
        self._seq = 0
        self._stop = threading.Event()
        self.dedup = True
        self.synced = 0
        self.last_error = ""
        self.last_ok = 0.0
        for i in range(tries):
            try:
                self._pull()
                break
            except TursoError as e:
                if e.http in (401, 403) or i == tries - 1:
                    raise
                log.warning("Turso: %s — повторю через %d с", e, 3 * (i + 1))
                time.sleep(3 * (i + 1))
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._loop, name="turso-sync", daemon=True)
            self._thread.start()
            atexit.register(self.close)
            _install_sigterm(self)

    # ── sqlite3-совместимость ──
    @property
    def row_factory(self):
        return self.local.row_factory

    @row_factory.setter
    def row_factory(self, f):
        self.local.row_factory = f

    @property
    def total_changes(self):
        return self.local.total_changes

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def execute(self, sql, params=()):
        with self._lock:
            cur = self.local.execute(sql, params)
            m = TX_RE.match(sql)
            if m:
                kw = m.group(1).upper()
                if kw == "BEGIN":
                    self._tx = []
                elif kw in ("COMMIT", "END"):
                    if self._tx:
                        self._enqueue(self._tx)
                    self._tx = None
                else:
                    self._tx = None
            elif WRITE_RE.match(sql):
                item = (sql, dict(params) if isinstance(params, dict) else tuple(params))
                if self._tx is not None:
                    self._tx.append(item)
                else:
                    self._enqueue([item])
            return cur

    def executemany(self, sql, seq):
        for p in seq:
            self.execute(sql, p)

    def executescript(self, script):
        with self._lock:
            self.local.executescript(script)
            items = [(s, ()) for s in split_sql(script) if WRITE_RE.match(s)]
            if items:
                self._enqueue(items)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # ── очередь ──
    def _enqueue(self, items):
        if self.dedup and len(items) == 1 and KV_RE.match(items[0][0]) and items[0][1]:
            key = items[0][1][0]
            for i in range(len(self._queue) - 1, -1, -1):      # старое значение того же ключа больше не нужно
                g = self._queue[i]
                if len(g) != 1 or not KV_RE.match(g[0][0]):
                    break
                if g[0][1] and g[0][1][0] == key:
                    del self._queue[i]
                    break
        self._queue.append(list(items))

    def pending(self):
        with self._lock:
            return sum(len(g) for g in self._queue) + (len(self._inflight[1]) if self._inflight else 0)

    # ── загрузка копии из Turso ──
    def _pull(self):
        r = self.remote
        self.protected = True
        try:
            res = r.pipeline([{"type": "execute", "stmt": stmt(s)} for s in SYNC_SQL])
            for x in res:
                Remote._result(x)
        except TursoError as e:
            if e.http:
                raise
            self.protected = False
            log.warning("Turso: без защиты от повторов (%s)", e)
        try:
            _, rows = r.rows("SELECT seq FROM _sync WHERE id=1")
            remote_seq = int(rows[0][0]) if rows else 0
        except TursoError:
            remote_seq = 0
        self._seq = max(remote_seq, int(time.time() * 1000))
        _, objs = r.rows("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL AND "
                         "name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_sync%' ESCAPE '\\' AND "
                         "type IN ('table', 'index', 'trigger', 'view')")
        order = {"table": 0, "index": 1, "view": 2, "trigger": 3}
        objs.sort(key=lambda o: order.get(o[0], 9))
        loc = self.local
        n_rows = 0
        for typ, name, _tbl, sql in objs:
            try:
                loc.execute(sql)
            except sqlite3.OperationalError as e:
                if "already exists" not in str(e):
                    raise
            if typ != "table":
                continue
            last = None
            while True:
                q = f'SELECT rowid AS "__rid", * FROM "{name}"' + (" WHERE rowid > ?" if last is not None else "") + \
                    f" ORDER BY rowid LIMIT {PAGE_ROWS}"
                cols, rows = r.rows(q, (last,) if last is not None else ())
                if not rows:
                    break
                names = ", ".join(['rowid'] + ['"' + c.replace('"', '""') + '"' for c in cols[1:]])
                loc.executemany(f'INSERT OR REPLACE INTO "{name}"({names}) VALUES({", ".join("?" * len(cols))})', rows)
                n_rows += len(rows)
                last = rows[-1][0]
                if len(rows) < PAGE_ROWS:
                    break
        try:
            _, seqs = r.rows("SELECT name, seq FROM sqlite_sequence")
            if seqs:
                loc.execute("DELETE FROM sqlite_sequence")
                loc.executemany("INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)", seqs)
        except (TursoError, sqlite3.OperationalError):
            pass
        self.last_ok = time.time()
        log.info("Turso: копия загружена — таблиц %d, строк %d", sum(1 for o in objs if o[0] == "table"), n_rows)

    # ── отправка изменений ──
    def flush(self):
        """Отправить накопленное. True — всё ушло, False — нет связи (повторим позже)."""
        while True:
            with self._lock:
                if self._inflight is None:
                    if not self._queue:
                        return True
                    take, n = 0, 0
                    while take < len(self._queue) and (take == 0 or n + len(self._queue[take]) <= MAX_BATCH):
                        n += len(self._queue[take])
                        take += 1
                    ops = [op for g in self._queue[:take] for op in g]
                    del self._queue[:take]
                    self._seq = max(self._seq + 1, int(time.time() * 1000))
                    self._inflight = (self._seq, ops)
                seq, ops = self._inflight
            try:
                status, bad = self._send(seq, ops)
            except TursoError as e:
                self.last_error = str(e)
                return False
            with self._lock:
                if status == "bad":
                    sql, args = ops[bad]
                    log.error("Turso не принял команду, пропускаю её: %s · %s", sql[:200], self.last_error)
                    ops = ops[:bad] + ops[bad + 1:]
                    self._inflight = (seq, ops) if ops else None
                    continue
                self._inflight = None
                self.synced += len(ops)
                self.last_ok = time.time()
                self.last_error = ""

    def _send(self, seq, ops):
        steps = [("BEGIN", (), None)]
        if self.protected:
            steps.append(("UPDATE _sync SET seq=? WHERE id=1", (seq,), {"type": "ok", "step": 0}))
        first = len(steps)
        for sql, args in ops:
            steps.append((sql, args, {"type": "ok", "step": len(steps) - 1}))
        commit = len(steps)
        steps.append(("COMMIT", (), {"type": "ok", "step": commit - 1}))
        steps.append(("ROLLBACK", (), {"type": "not", "cond": {"type": "ok", "step": commit}}))
        results, errors = self.remote.batch(steps)
        errors = list(errors) + [None] * (len(steps) - len(errors))
        results = list(results) + [None] * (len(steps) - len(results))
        if results[commit] is not None and errors[commit] is None:
            return "ok", None
        for i, err in enumerate(errors[:commit]):
            if err:
                msg = (err or {}).get("message") or ""
                self.last_error = msg
                if self.protected and i == 1 and DUP in msg:
                    return "dup", None           # эта пачка уже записана раньше
                if i >= first:
                    return "bad", i - first
                raise TursoError("Turso: " + msg)
        raise TursoError("Turso не подтвердил запись: " + str(errors[commit] or ""))

    def _loop(self):
        wait = self.flush_every
        while not self._stop.wait(wait):
            try:
                ok = self.flush()
            except Exception as e:          # pragma: no cover — на всякий случай не роняем поток
                log.exception("Turso: сбой отправки")
                self.last_error, ok = repr(e), False
            if ok:
                wait = self.flush_every
            else:
                if wait == self.flush_every:
                    log.warning("Turso: %s — изменения подождут (в очереди %d)", self.last_error, self.pending())
                wait = min(30.0, max(self.flush_every, wait * 2))

    def close(self, timeout=20):
        if self._stop.is_set():
            return
        self._stop.set()
        end = time.time() + timeout
        with self._lock:
            if self._tx:
                self._enqueue(self._tx)
                self._tx = None
        while self.pending() and time.time() < end:
            if self.flush():
                break
            time.sleep(1)
        if self.pending():
            log.error("Turso: не успел отправить %d изменений", self.pending())


class _Cursor:
    def __init__(self, conn):
        self.conn, self.cur = conn, None

    def execute(self, sql, params=()):
        self.cur = self.conn.execute(sql, params)
        return self

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def __iter__(self):
        return iter(self.cur)

    @property
    def lastrowid(self):
        return self.cur.lastrowid

    @property
    def rowcount(self):
        return self.cur.rowcount


_OPEN = []


def _install_sigterm(conn):
    """Render при перезапуске шлёт SIGTERM — успеть отправить последние изменения."""
    _OPEN.append(conn)
    if len(_OPEN) > 1 or threading.current_thread() is not threading.main_thread():
        return
    try:
        prev = signal.getsignal(signal.SIGTERM)
    except (ValueError, AttributeError):
        return

    def on_term(sig, frame):
        for c in list(_OPEN):
            try:
                c.close()
            except Exception:
                pass
        if callable(prev) and prev not in (signal.SIG_DFL, signal.SIG_IGN):
            prev(sig, frame)
        os._exit(0)

    try:
        signal.signal(signal.SIGTERM, on_term)
    except (ValueError, OSError):
        pass


def connect(path, token=None, **kw):
    if is_remote(path):
        return Connection(path, token if token is not None else os.environ.get("DB_TOKEN", ""), **kw)
    return sqlite3.connect(path, **kw)
