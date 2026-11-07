"""Метроном · общий бот (один на всех).

Человек подключает свой бесплатный сервер (Render, кнопка «Создать сервер»), входит там в Telegram —
и дальше настраивает рассылку здесь, кнопками. Рассылка идёт с его сервера, бот только командует и будит.

Бот хранит: адрес сервера, ключ управления и зашифрованную копию входа (расшифровать её бот не может —
ключ шифрования лежит только в Render у пользователя). В приписке к сообщениям — реклама из магазина.

Настройки: config.env рядом или переменные окружения (BOT_TOKEN, REPO=github_user/metronom, ADMIN_CODE, PRICE).
База: DB_URL + DB_TOKEN — облачная Turso (для Render); без них — файл hub.db рядом (для VPS).
Только стандартный Python 3.10+.
"""
import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import ads_bot
from ads_bot import AD_PREFIX, ApiError, BotAPI, d as dd, dn, kb, u16

VERSION = "2.2"
HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.append(_p)
try:
    import tursodb                      # облачная база (metronom/tursodb.py), без библиотек
except ImportError:                     # pragma: no cover
    tursodb = None
PING_EVERY = 5 * 60          # будить сервер чаще, чем Render усыпляет (15 мин)
RETRY = 60
DOWN_ALERT = 60 * 60
PAGE = 8
BACKUP_TAG = "#metronom_hub_backup"
INTERVALS = [(600, "10 мин"), (900, "15 мин"), (1800, "30 мин"), (3600, "1 ч"), (7200, "2 ч"), (10800, "3 ч"),
             (21600, "6 ч"), (43200, "12 ч"), (86400, "24 ч")]
_B2N = {"bold": "Bold", "italic": "Italic", "underline": "Underline", "strikethrough": "Strike", "code": "Code",
        "spoiler": "Spoiler", "text_link": "TextUrl", "url": "Url", "mention": "Mention"}
ERRORS = {
    "not_connected": "Сначала подключи Метроном: /start → 🔌 Подключить.",
    "not_running": "Сервер только что перезапустился — восстанавливаю вход. Попробуй через минуту.",
    "down": "Сервер не отвечает. Если он спал — проснётся за минуту, нажми ещё раз. Долго не отвечает — загляни в Render.",
    "bad_key": "Сервер не принимает ключ бота. Открой свой сервер и войди в Telegram заново — бот переподключится сам.",
    "no_ad": "Сначала задай рекламу: 📝 Реклама.",
    "no_chats": "Добавь чаты: 💬 Чаты.",
}
log = logging.getLogger("hub")


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} с"
    dys, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    out = ([f"{dys} д"] if dys else []) + ([f"{h} ч"] if h else []) + ([f"{m} мин"] if m and not dys else [])
    return " ".join(out) or "0 мин"


_UNITS = {"d": 86400, "д": 86400, "h": 3600, "ч": 3600, "m": 60, "м": 60, "мин": 60}


def parse_interval(s):
    s = (s or "").strip().lower().replace(" ", "")
    if s.isdigit():
        return int(s) * 60
    total, pos = 0, 0
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)(мин|d|д|h|ч|m|м)", s):
        if m.start() != pos:
            return None
        total += float(m.group(1).replace(",", ".")) * _UNITS[m.group(2)]
        pos = m.end()
    return int(total) if s and pos == len(s) and total > 0 else None


def load_env(path):
    return ads_bot.load_env(path)


# ───────────────────────── база ─────────────────────────

class DB:
    COLS = ("uid", "name", "url", "key", "blob", "ver", "info", "last_ok", "fails", "next_ping", "ack", "alert",
            "sent_prev", "created", "seen")

    def __init__(self, path, token=None):
        if tursodb and tursodb.is_remote(path):
            self.c = tursodb.Connection(path, token or "")
        elif "://" in str(path):
            raise RuntimeError("Для облачной базы нужен файл metronom/tursodb.py")
        else:
            self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
            CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, name TEXT, url TEXT, key TEXT, blob TEXT, ver TEXT,
              info TEXT, last_ok REAL DEFAULT 0, fails INTEGER DEFAULT 0, next_ping REAL DEFAULT 0, ack INTEGER DEFAULT 0,
              alert TEXT DEFAULT '', sent_prev INTEGER DEFAULT 0, created REAL, seen REAL);
            CREATE INDEX IF NOT EXISTS users_url ON users(url);
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);""")

    def user(self, uid):
        r = self.c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
        return dict(r) if r else None

    def rows(self, sql="SELECT * FROM users", args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def upsert(self, uid, **f):
        f = {k: v for k, v in f.items() if k in self.COLS and k != "uid"}
        if not self.user(uid):
            self.c.execute("INSERT INTO users(uid, created) VALUES(?, ?)", (uid, time.time()))
        if f:
            self.c.execute(f"UPDATE users SET {', '.join(k + '=?' for k in f)} WHERE uid=?", (*f.values(), uid))
        self.c.commit()

    def get(self, k, default=None):
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v, ensure_ascii=False)))
        self.c.commit()

    def dump(self):
        return {"users": self.rows(), "kv": {r[0]: json.loads(r[1]) for r in self.c.execute("SELECT k, v FROM kv")}}

    def load(self, d):
        for u in d.get("users", []):
            u = dict(u)
            self.upsert(u.pop("uid"), **u)
        for k, v in d.get("kv", {}).items():
            self.set(k, v)


# ───────────────────────── магазин рекламы внутри бота ─────────────────────────

class HubShop(ads_bot.Shop):
    """Заказы рекламы хранятся в базе бота (kv «shop») — на Render это облако Turso, файлы там стираются."""

    def __init__(self, hub, *a, **kw):
        super().__init__(*a, **kw)
        self.hub = hub

    def load(self):
        st = self.hub.db.get("shop")
        if isinstance(st, dict):
            self.st.update(st)
            return True
        if super().load():              # один раз перенести старый ads.json в базу
            self.save()
            return True
        return False

    def save(self):
        self.hub.db.set("shop", self.st)
        self.dirty = True

    async def sync(self, force=False):
        return self.hub.update_footer()

    async def backup(self):
        pass

    async def restore(self):
        return False

    async def cmd_start(self, uid, arg, m):
        await self.hub.shop_screen(uid)

    cmd_help = cmd_start

    async def adm_admin(self, arg):
        await self.hub.adm_help()


# ───────────────────────── бот ─────────────────────────

class Hub:
    def __init__(self, api, db, cfg, shop_path, today=ads_bot.utc_today, node_call=None, pool=None):
        self.api, self.db, self.cfg = api, db, cfg
        self.bot_username = cfg.get("BOT_USERNAME") or "metronom_tg_bot"
        self.repo = (cfg.get("REPO") or "").strip().strip("/")
        self.allow_http = cfg.get("ALLOW_HTTP") == "1"
        self.admin = int(db.get("admin") or cfg.get("ADMIN_ID") or 0) or None
        self.today = today
        self.pool = pool or ThreadPoolExecutor(32)
        self.node_call = node_call or self._node_call
        self.steps, self.cache = {}, {}
        self.inflight = set()
        self.ulocks = {}
        self.connect_times = {}
        self.footer = db.get("footer")
        self.shop = HubShop(self, api, self.admin, int(cfg.get("PRICE") or 50), shop_path, today)
        self.shop.load()
        self.shop.username = self.bot_username
        self.update_footer()

    # ── мелочи ──
    async def q(self, method, **p):
        try:
            return await self.api(method, **p)
        except Exception as e:
            log.info("%s: %s", method, e)
            return None

    async def send(self, uid, text, **p):
        return await self.q("sendMessage", chat_id=uid, text=text[:4096], link_preview_options={"is_disabled": True}, **p)

    async def show(self, uid, text, markup=None, mid=None):
        markup = markup or {"inline_keyboard": []}
        if mid:
            try:
                await self.api("editMessageText", chat_id=uid, message_id=mid, text=text[:4096], reply_markup=markup,
                               link_preview_options={"is_disabled": True})
                return
            except ApiError as e:
                if "not modified" in e.desc:
                    return
        await self.send(uid, text, reply_markup=markup)

    def deploy_url(self):
        return f"https://render.com/deploy?repo=https://github.com/{self.repo}" if self.repo else None

    def base_footer(self):
        return self.db.get("base_footer") or f"⏱ Отправлено через Метроном — бесплатная авторассылка: @{self.bot_username}"

    def update_footer(self):
        base = self.base_footer()
        self.shop.base_footer = base
        live = self.shop.live()
        text, ents, until = base, [], None
        if live:
            head = base + "\n" + AD_PREFIX
            text, shift, until = head + live["text"], u16(head), live["end"]
            for e in live.get("entities") or []:
                if e["type"] in _B2N:
                    x = {"t": _B2N[e["type"]], "o": e["offset"] + shift, "l": e["length"]}
                    if e.get("url"):
                        x["u"] = e["url"]
                    ents.append(x)
        f = {"text": text, "ents": ents, "ad_until": until, "base": base}
        if f != self.footer:
            self.footer = f
            self.db.set("footer", f)
            return True
        return False

    def seen(self, fr):
        name = ("@" + fr["username"]) if fr.get("username") else (fr.get("first_name") or "")
        self.db.upsert(fr["id"], name=name, seen=time.time())

    def info(self, u):
        try:
            return json.loads(u.get("info") or "{}")
        except ValueError:
            return {}

    # ── связь с серверами пользователей ──
    def _http(self, url, body, key, timeout):
        req = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode(),
                                     {"Content-Type": "application/json", "X-Key": key or "", "User-Agent": "MetronomHub/2"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        except Exception as e:
            return 0, {"error": e.__class__.__name__}
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, None          # Render показывает «просыпаюсь» — не JSON

    async def _node_call(self, url, path, body, key, timeout=40):
        return await asyncio.get_running_loop().run_in_executor(self.pool, self._http, url + path, body, key, timeout)

    async def do(self, uid, action, timeout=45, **kw):
        u = self.db.user(uid)
        if not u or not u.get("url"):
            return None, "not_connected"
        code, res = await self.node_call(u["url"], "/api/do", dict(kw, action=action), u["key"], timeout)
        if code == 200 and isinstance(res, dict):
            if res.get("stats"):
                self._stats(u, res["stats"], res.get("me"))
            if not res.get("ok") and res.get("error") == "not_running":
                self.db.upsert(uid, next_ping=0)
                return None, "not_running"
            return res, None
        return None, "bad_key" if code == 403 else "down"

    def _stats(self, u, stats, me=None, state=None, v=None):
        inf = self.info(u)
        inf["stats"] = stats
        if me:
            inf["me"] = me
        if state:
            inf["state"] = state
        if v:
            inf["v"] = v
        total = int(stats.get("sent_total") or 0)
        prev = int(u.get("sent_prev") or 0)
        if total > prev:
            k = "sent:" + self.today().isoformat()
            self.db.set(k, int(self.db.get(k, 0)) + total - prev)
        self.db.upsert(u["uid"], info=json.dumps(inf, ensure_ascii=False), sent_prev=total)
        u["sent_prev"], u["info"] = total, json.dumps(inf, ensure_ascii=False)

    async def alert(self, uid, flag, text, markup=None):
        u = self.db.user(uid) or {}
        flags = set(filter(None, (u.get("alert") or "").split(",")))
        if flag in flags:
            return
        flags.add(flag)
        self.db.upsert(uid, alert=",".join(sorted(flags)))
        await self.send(uid, text, reply_markup=markup)

    def server_btn(self, u):
        return kb() if not u.get("url") else {"inline_keyboard": [[{"text": "🌐 Открыть мой сервер", "url": u["url"]}]]}

    async def ping(self, u):
        uid, now = u["uid"], time.time()
        body = {"footer": self.footer, "ver": u.get("ver"), "ack": u.get("ack") or 0}
        code, res = await self.node_call(u["url"], "/api/ping", body, u["key"], 90)
        if code == 200 and isinstance(res, dict) and res.get("ok"):
            return await self._on_ping(u, res)
        if code == 403:
            self.db.upsert(uid, next_ping=now + 1800, fails=(u.get("fails") or 0) + 1)
            return await self.alert(uid, "badkey", "⚠️ " + ERRORS["bad_key"], self.server_btn(u))
        fails = (u.get("fails") or 0) + 1
        self.db.upsert(uid, fails=fails, next_ping=now + (RETRY if fails < 10 else PING_EVERY))
        if u.get("last_ok") and now - u["last_ok"] > DOWN_ALERT:
            await self.alert(uid, "down", "⚠️ Твой сервер Метронома не отвечает больше часа — рассылка стоит.\n\n"
                                          "Проверь в Render: сервис не удалён и не приостановлен? Бесплатно даётся 750 часов "
                                          "в месяц на аккаунт — если там есть другие бесплатные сервисы, часы могли кончиться.",
                             self.server_btn(u))

    async def _on_ping(self, u, res):
        uid, now = u["uid"], time.time()
        if res.get("owner") and res["owner"] != uid:
            self.db.upsert(uid, next_ping=now + 3600)
            return await self.alert(uid, "foreign", "⚠️ На твоём сервере сейчас вошёл другой аккаунт Telegram. "
                                                    "Если это не ты — удали сервер в Render и подключи новый.")
        f = {"last_ok": now, "fails": 0, "next_ping": now + PING_EVERY}
        if res.get("blob"):
            f.update(blob=res["blob"], ver=res["ver"])
        out = res.get("outbox") or []
        for m in out:
            await self.send(uid, m["text"])
        if out:
            f["ack"] = max(m["id"] for m in out)
        self.db.upsert(uid, **f)
        u.update(f)
        if res.get("stats"):
            self._stats(u, res["stats"], res.get("me"), res.get("state"), res.get("v"))
        else:
            inf = self.info(u)
            inf.update(state=res.get("state"), v=res.get("v"))
            self.db.upsert(uid, info=json.dumps(inf, ensure_ascii=False))
        st = res.get("state")
        if st == "running":
            self.db.upsert(uid, alert="")
        elif st == "new" and u.get("blob"):
            await self.restore(self.db.user(uid))
        else:
            await self.alert(uid, "relogin", "⚠️ Вход на твоём сервере слетел — рассылка стоит. Открой сервер и войди "
                                             "в Telegram заново тем же аккаунтом. Настройки сохранились.", self.server_btn(u))

    async def restore(self, u):
        code, res = await self.node_call(u["url"], "/api/restore", {"blob": u["blob"], "footer": self.footer}, u["key"], 90)
        if code == 200 and isinstance(res, dict):
            if res.get("ok"):
                log.info("восстановил вход %s", u["uid"])
                return await self._on_ping(u, res)
            if res.get("error") in ("session_dead", "bad_blob", "owner_mismatch"):
                self.db.upsert(u["uid"], blob=None if res["error"] == "bad_blob" else u["blob"])
                await self.alert(u["uid"], "relogin", "⚠️ Telegram сбросил вход на твоём сервере (вышли из сессии в "
                                                      "«Устройствах» или пароль сменили). Открой сервер и войди заново — "
                                                      "настройки рассылки сохранились.", self.server_btn(u))

    async def pinger(self):
        while True:
            try:
                now = time.time()
                for u in self.db.rows("SELECT * FROM users WHERE url IS NOT NULL AND url != '' AND next_ping <= ?", (now,)):
                    if u["uid"] in self.inflight or len(self.inflight) >= 24:
                        continue
                    self.inflight.add(u["uid"])
                    t = asyncio.ensure_future(self.ping(u))
                    t.add_done_callback(lambda _t, uid=u["uid"]: (self.inflight.discard(uid), _t.exception() and
                                                                 log.error("ping %s: %r", uid, _t.exception())))
            except Exception:
                log.exception("pinger")
            await asyncio.sleep(10)

    # ── входящие ──
    async def handle(self, up):
        if "pre_checkout_query" in up:
            async with self.shop._lock:
                return await self.shop.on_precheckout(up["pre_checkout_query"])
        if "callback_query" in up:
            uid = up["callback_query"]["from"]["id"]
            async with self.ulocks.setdefault(uid, asyncio.Lock()):
                return await self.on_button(up["callback_query"])
        m = up.get("message")
        if m and m.get("chat", {}).get("type") == "private" and m.get("from"):
            async with self.ulocks.setdefault(m["from"]["id"], asyncio.Lock()):
                return await self.on_message(m)

    async def on_message(self, m):
        uid = m["from"]["id"]
        self.seen(m["from"])
        if "successful_payment" in m:
            async with self.shop._lock:
                return await self.shop.on_paid(m)
        text = m.get("text") or ""
        c = re.match(r"^/connect\s+(\S+)\s+([0-9a-f]{40})\s*$", text)
        if c:
            return await self.on_connect(m, uid, c.group(1), c.group(2))
        cmd = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", text, re.S)
        if cmd:
            name, arg = cmd.group(1).lower(), cmd.group(2).strip()
            self.steps.pop(uid, None)
            if name == "admin":
                return await self.adm_claim(uid, arg)
            if uid == self.admin:
                if hasattr(self, "adm_" + name):
                    return await getattr(self, "adm_" + name)(arg, m)
                if hasattr(self.shop, "adm_" + name):
                    async with self.shop._lock:
                        return await getattr(self.shop, "adm_" + name)(arg)
            if name in ("buy", "my", "paysupport", "terms"):
                async with self.shop._lock:
                    if name == "buy":
                        self.shop.users.pop(uid, None)
                    return await getattr(self.shop, "cmd_" + name)(uid, arg, m)
            self.shop.users.pop(uid, None)
            if name == "start" and arg == "ads":
                return await self.shop_screen(uid)
            if name in ("start", "menu", "status", "cancel"):
                return await self.home(uid)
            if name == "help":
                return await self.help(uid)
            if name == "support":
                return await self.ask_support(uid)
            if name in ("on", "off", "now"):
                return await self.btn_simple(uid, name, None)
            if name == "every":
                sec = parse_interval(arg)
                return await (self.set_interval(uid, sec, None) if sec else self.interval_screen(uid))
            if name in ("text", "ad"):
                return await self.ad_screen(uid)
            if name in ("chats", "groups"):
                return await (self.chats_screen(uid) if name == "chats" else self.groups_screen(uid, 0))
            return await self.home(uid)
        if uid in self.shop.users:
            async with self.shop._lock:
                return await self.shop.on_message(m)
        step = self.steps.get(uid)
        if step:
            return await self.on_step(uid, step, m)
        await self.home(uid)

    async def on_button(self, q):
        uid, data = q["from"]["id"], q.get("data") or ""
        mid = (q.get("message") or {}).get("message_id")
        self.seen(q["from"])
        if data in ("buy", "my", "send") or data.startswith(("days:", "ok:", "no:")):
            self.steps.pop(uid, None)
            async with self.shop._lock:
                return await self.shop.on_button(q)
        await self.q("answerCallbackQuery", callback_query_id=q["id"])
        self.steps.pop(uid, None)
        self.shop.users.pop(uid, None)
        if data in ("start", "m"):
            return await self.home(uid, mid)
        simple = {"c": self.connect_screen, "help": self.help, "shop": self.shop_screen, "ad": self.ad_screen,
                  "ch": self.chats_screen, "iv": self.interval_screen, "more": self.more_screen, "srv": self.server_screen,
                  "lo": self.logout_confirm}
        if data in simple:
            return await simple[data](uid, mid)
        if data in ("on", "off", "now", "test"):
            return await self.btn_simple(uid, data, mid)
        if data == "lo!":
            return await self.logout(uid, mid)
        if data == "cl":
            self.steps[uid] = {"step": "link"}
            return await self.show(uid, "🔗 Пришли ссылки на чаты: t.me/chat или @chat, можно несколько через пробел.\n"
                                        "Тема форума — ссылкой на тему: t.me/chat/5.\nТы должен состоять в этих чатах.",
                                   kb([("↩️ Отмена", "ch")]), mid)
        if data == "gs":
            self.steps[uid] = {"step": "gsearch"}
            return await self.show(uid, "🔍 Пришли слово из названия группы.", kb([("↩️ Отмена", "gp:0")]), mid)
        if data == "ic":
            self.steps[uid] = {"step": "interval"}
            return await self.show(uid, "✏️ Пришли интервал: 45m, 2h, 1h30m или число минут.", kb([("↩️ Отмена", "iv")]), mid)
        if data == "ce":
            res, err = await self.do(uid, "chat_on")
            return await self.chats_screen(uid, mid, note=ERRORS.get(err, "") if err else
                                           f"🔁 Включил: {len(res['changed'])}")
        m = re.fullmatch(r"(gp|gt|rm|rd|is):(\d+)", data)
        if m:
            k, n = m.group(1), int(m.group(2))
            if k == "gp":
                return await self.groups_screen(uid, n, mid, refresh=n == 0 and not self.cache.get(uid, {}).get("q"))
            if k == "gt":
                return await self.toggle_group(uid, n, mid)
            if k == "rm":
                return await self.remove_screen(uid, n, mid)
            if k == "rd":
                return await self.remove_chat(uid, n, mid)
            if k == "is":
                return await self.set_interval(uid, n, mid)

    async def on_step(self, uid, step, m):
        s = step["step"]
        text = (m.get("text") or "").strip()
        if s == "ad":
            return await self.set_ad(uid, m)
        if s == "support":
            self.steps.pop(uid, None)
            u = self.db.user(uid) or {}
            await self.send(self.admin, f"🆘 Вопрос от {u.get('name') or uid} (id {uid}):\n\n{text or '[не текст]'}\n\n"
                                        f"Ответить: /reply {uid} текст") if self.admin else None
            return await self.send(uid, "Передал. Ответ придёт сюда.")
        if s == "broadcast" and uid == self.admin:
            self.steps.pop(uid, None)
            return await self.broadcast(m)
        if not text:
            return await self.send(uid, "Нужен текст. /menu — в меню.")
        if s == "link":
            self.steps.pop(uid, None)
            res, err = await self.do(uid, "add", link=text, timeout=60)
            return await self.chats_screen(uid, note=self._add_note(res, err))
        if s == "gsearch":
            self.steps.pop(uid, None)
            self.cache.setdefault(uid, {})["q"] = text
            return await self.groups_screen(uid, 0, refresh=True)
        if s == "interval":
            sec = parse_interval(text)
            if not sec:
                return await self.send(uid, "Не понял. Примеры: 45m · 2h · 1h30m · 90")
            self.steps.pop(uid, None)
            return await self.set_interval(uid, sec, None)

    # ── экраны ──
    async def home(self, uid, mid=None, note=""):
        u = self.db.user(uid)
        if not u or not u.get("url"):
            return await self.welcome(uid, mid)
        res, err = await self.do(uid, "status")
        inf = self.info(self.db.user(uid))
        st = (res or {}).get("stats") or inf.get("stats") or {}
        me = (res or {}).get("me") or inf.get("me") or {}
        lines = [f"⏱ Метроном · {me.get('name') or 'твоя рассылка'}" + (f" (@{me['username']})" if me.get("username") else "")]
        if res:
            self.cache.setdefault(uid, {})["chats"] = res["chats"]
            off = sum(1 for c in res["chats"] if not c["on"])
            ad = res.get("ad")
            lines += ["▶️ Рассылка работает" if st.get("enabled") else "⏸ Рассылка выключена",
                      "📝 Реклама: " + ((("🖼 " if ad.get("kind") == "photo" else "") + "«" +
                                         (ad.get("preview") or "")[:60].replace("\n", " ") + "…»") if ad else "не задана"),
                      f"💬 Чатов: {st.get('on', 0)} из {st.get('chats', 0)}" + (f" · ⛔ отключено {off}" if off else ""),
                      f"🕐 Каждые {fmt_dur(st.get('interval', 3600))}" +
                      (f" · следующая {'сейчас' if not st.get('next_in') else 'через ' + fmt_dur(st['next_in'])}"
                       if st.get("enabled") and st.get("next_in") is not None else ""),
                      f"📨 Отправлено всего: {st.get('sent_total', 0)}"]
            if st.get("pause"):
                lines.append(f"⏳ Telegram попросил паузу: ещё {fmt_dur(st['pause'])}")
        else:
            lines += ["", "⚠️ " + ERRORS.get(err, err)]
        if note:
            lines += ["", note]
        on = st.get("enabled")
        markup = kb([("📝 Реклама", "ad"), ("💬 Чаты", "ch")],
                    [("🕐 Интервал", "iv"), ("⏸ Выключить", "off") if on else ("▶️ Включить", "on")],
                    [("🚀 Разослать сейчас", "now"), ("👁 Проверить", "test")],
                    [("🔄 Обновить", "m"), ("⚙️ Ещё", "more")])
        await self.show(uid, "\n".join(lines), markup, mid)

    async def welcome(self, uid, mid=None):
        await self.show(uid, "⏱ Метроном — бесплатная авторассылка в Telegram-чаты\n\n"
                             "• рассылает твою рекламу по чатам от твоего аккаунта — по расписанию, 24/7\n"
                             "• компьютер не нужен: работает на твоём бесплатном сервере\n"
                             "• всё настраивается здесь, кнопками\n\n"
                             "Подключение — 3 минуты, один раз.",
                        kb([("🔌 Подключить", "c")], [("💰 Реклама в приписке", "shop")], [("❓ Как это работает", "help")]),
                        mid)

    async def connect_screen(self, uid, mid=None):
        url = self.deploy_url()
        if not url:
            return await self.show(uid, "Подключение пока не настроено владельцем бота (нет REPO). Загляни позже.",
                                   kb([("↩️ Назад", "start")]), mid)
        text = ("🔌 Подключение — 3 минуты\n\n"
                "1️⃣ Нажми «Создать сервер» → войди в Render через GitHub или Google → нажми «Deploy Blueprint» "
                "(или «Apply»). Это бесплатно.\n\n"
                "2️⃣ Подожди 2–3 минуты, пока сервер соберётся. Открой сервис «metronom» — сверху будет его ссылка "
                "вида metronom-xxxx.onrender.com. Открой её.\n\n"
                "3️⃣ На странице сервера впиши свои ключи Telegram (api_id и api_hash — берутся за 2 минуты "
                "на my.telegram.org, инструкция там же) и войди в Telegram — по номеру и коду или QR-кодом.\n\n"
                "Всё: бот сам увидит подключение и пришлёт меню.\n\n"
                "🔒 Вход хранится на твоём сервере. У бота — только зашифрованная копия для перезапусков, "
                "ключ от неё есть только у тебя.\n"
                "⚠️ Лучше отдельный аккаунт: за частые рассылки Telegram может ограничить аккаунт.")
        await self.show(uid, text, {"inline_keyboard": [[{"text": "🌐 Создать сервер", "url": url}],
                                                        [{"text": "↩️ Назад", "callback_data": "start"}]]}, mid)

    async def help(self, uid, mid=None):
        await self.show(uid, "❓ Как это работает\n\n"
                             "Метроном отправляет твоё рекламное сообщение в выбранные чаты каждые N минут/часов — "
                             "от твоего аккаунта, как будто ты пишешь сам.\n\n"
                             "Где работает: на твоём личном бесплатном сервере в Render. Бот — пульт: он передаёт серверу "
                             "команды и будит его, чтобы тот не засыпал. Читать твои переписки бот не может — у него "
                             "нет такой команды, код открыт.\n\n"
                             "Бесплатно, потому что под каждым сообщением короткая приписка Метронома (иногда — со строкой "
                             "рекламы).\n\n"
                             "Сам бот:\n• ждёт медленный режим чатов\n• отключает чаты, где писать нельзя, и пишет почему\n"
                             "• если Telegram просит паузу — ставит паузу; если ограничил за спам — выключает всё\n\n"
                             "Чтобы аккаунт жил дольше: интервал от 30 минут, только чаты, где реклама разрешена, "
                             "отдельный аккаунт.\n\nВопрос владельцу: /support",
                        kb([("↩️ Назад", "start")]), mid)

    async def more_screen(self, uid, mid=None):
        await self.show(uid, "⚙️ Ещё", kb([("💰 Купить рекламу в приписке", "shop")], [("🖥 Мой сервер", "srv"),
                                                                                       ("❓ Помощь", "help")],
                                          [("🚪 Отключить аккаунт", "lo"), ("↩️ Меню", "m")]), mid)

    async def server_screen(self, uid, mid=None):
        u = self.db.user(uid) or {}
        inf = self.info(u)
        ago = fmt_dur(time.time() - u["last_ok"]) + " назад" if u.get("last_ok") else "ещё не отвечал"
        await self.show(uid, f"🖥 Мой сервер\n{u.get('url') or '—'}\n\nСостояние: {inf.get('state') or '?'} · "
                             f"на связи: {ago} · версия {inf.get('v') or '?'}\n\n"
                             "Обновить до новой версии: Render → сервис metronom → Manual Deploy → Deploy latest commit. "
                             "Вход и настройки сохранятся.",
                        {"inline_keyboard": ([[{"text": "🌐 Открыть сервер", "url": u["url"]}]] if u.get("url") else [])
                         + [[{"text": "↩️ Назад", "callback_data": "more"}]]}, mid)

    async def logout_confirm(self, uid, mid=None):
        await self.show(uid, "🚪 Отключить аккаунт?\nСервер выйдет из твоего Telegram, рассылка остановится, "
                             "копия входа у бота удалится.", kb([("✅ Да, отключить", "lo!")], [("↩️ Назад", "more")]), mid)

    async def logout(self, uid, mid=None):
        await self.do(uid, "logout", timeout=30)
        self.db.upsert(uid, url=None, key=None, blob=None, ver=None, info=None, alert="")
        await self.show(uid, "🚪 Отключено: сервер вышел из аккаунта, копия входа удалена.\n"
                             "Сам сервер можно удалить в Render: сервис → Settings → Delete Web Service.",
                        kb([("🔌 Подключить снова", "c")]), mid)

    async def ad_screen(self, uid, mid=None):
        res, err = await self.do(uid, "status")
        if err == "not_connected":
            return await self.welcome(uid, mid)
        ad = (res or {}).get("ad")
        self.steps[uid] = {"step": "ad"}
        await self.show(uid, "📝 Реклама\n\nСейчас: " + (("«" + ad["preview"][:200] + "»") if ad else "не задана") +
                        "\n\nПришли сюда новое сообщение с рекламой — текст или фото с подписью. Жирный шрифт, ссылки и "
                        "эмодзи сохранятся. Внизу сама добавится короткая приписка Метронома.",
                        kb([("↩️ Отмена", "m")]), mid)

    async def set_ad(self, uid, m):
        photo = m.get("photo")
        if any(k in m for k in ("video", "animation", "document", "sticker", "voice", "audio", "video_note")):
            return await self.send(uid, "Пока можно только текст или одно фото с подписью. Пришли ещё раз.")
        text = (m.get("caption") if photo else m.get("text")) or ""
        ents = (m.get("caption_entities") if photo else m.get("entities")) or []
        extra = {}
        if photo:
            try:
                extra["photo"] = base64.b64encode(await self.api.download(photo[-1]["file_id"])).decode()
            except Exception as e:
                return await self.send(uid, f"Не смог скачать фото ({e.__class__.__name__}). Пришли ещё раз.")
        if not text.strip() and not photo:
            return await self.send(uid, "Пустое сообщение. Пришли текст рекламы.")
        await self.q("sendChatAction", chat_id=uid, action="typing")
        res, err = await self.do(uid, "ad", text=text, entities=ents, timeout=90, **extra)
        if err:
            return await self.send(uid, "⚠️ " + ERRORS.get(err, err))
        if not res.get("ok"):
            return await self.send(uid, {"caption_long": "Подпись к фото слишком длинная (лимит 1024 символа). Сократи.",
                                         "text_long": "Текст слишком длинный (лимит 4096 символов). Сократи."}
                                   .get(res.get("error"), "Не получилось: " + str(res.get("error"))))
        self.steps.pop(uid, None)
        await self.send(uid, "✅ Реклама сохранена. Копия лежит у тебя в «Избранном» — не удаляй её, рассылка берёт "
                             "рекламу оттуда. Так её увидят в чатах:")
        await self.preview(uid, res["preview"], photo[-1]["file_id"] if photo else None)
        await self.home(uid, note="" if res["preview"]["fits"] else
                        "⚠️ Приписка не влезла в лимит Telegram — сократи текст на пару строк.")

    async def preview(self, uid, pv, photo_id=None):
        text, ents = pv["text"], pv.get("entities") or []
        if pv.get("media") and not photo_id:
            pre = "🖼 [фото]\n"
            text, ents = pre + text, [dict(e, offset=e["offset"] + u16(pre)) for e in ents]
        for attempt in (ents, []):
            try:
                if photo_id:
                    await self.api("sendPhoto", chat_id=uid, photo=photo_id, caption=text[:1024], caption_entities=attempt)
                else:
                    await self.api("sendMessage", chat_id=uid, text=text[:4096], entities=attempt,
                                   link_preview_options={"is_disabled": True})
                return
            except ApiError as e:
                log.info("preview: %s", e.desc)

    async def chats_screen(self, uid, mid=None, note=""):
        res, err = await self.do(uid, "status")
        if err:
            return await self.show(uid, "⚠️ " + ERRORS.get(err, err), kb([("🔄 Ещё раз", "ch"), ("↩️ Меню", "m")]), mid)
        chats = res["chats"]
        self.cache.setdefault(uid, {})["chats"] = chats
        lines = [f"💬 Чаты в рассылке: {len(chats)}"]
        for i, c in enumerate(chats[:40], 1):
            if not c["on"]:
                s = "⛔ " + (c["err"] or "выключен")
            elif c.get("next_in") is None:
                s = f"⏸ отправлено {c['sent']}"
            else:
                s = f"отправлено {c['sent']} · " + ("сейчас" if not c["next_in"] else "через " + fmt_dur(c["next_in"]))
            lines.append(f"{i}. {'✅' if c['on'] else '⛔'} {c['title'][:45]} — {s}")
        if len(chats) > 40:
            lines.append(f"… и ещё {len(chats) - 40}")
        if not chats:
            lines.append("\nПока пусто. Добавь чаты из своих групп — ты должен в них состоять.")
        if note:
            lines += ["", note]
        rows = [[("➕ Из моих групп", "gp:0"), ("🔗 По ссылке", "cl")]]
        extra = ([("🔁 Включить отключённые", "ce")] if any(not c["on"] for c in chats) else []) + \
                ([("🗑 Убрать", "rm:0")] if chats else [])
        if extra:
            rows.append(extra)
        rows.append([("↩️ Меню", "m")])
        await self.show(uid, "\n".join(lines), kb(*rows), mid)

    async def groups_screen(self, uid, page, mid=None, refresh=True):
        cache = self.cache.setdefault(uid, {})
        if refresh or "groups" not in cache:
            res, err = await self.do(uid, "groups", timeout=60)
            if err:
                return await self.show(uid, "⚠️ " + ERRORS.get(err, err), kb([("🔄 Ещё раз", "gp:0"), ("↩️ Чаты", "ch")]), mid)
            gs = res["groups"]
            q = (cache.pop("q", None) or "").lower()
            if q:
                gs = [g for g in gs if q in g["title"].lower() or q in (g.get("username") or "").lower()]
            cache["groups"] = gs
        gs = cache["groups"]
        if not gs:
            return await self.show(uid, "Групп не найдено. Вступи в нужные чаты в Telegram и нажми «Обновить».",
                                   kb([("🔄 Обновить", "gp:0"), ("🔍 Поиск", "gs")], [("↩️ Чаты", "ch")]), mid)
        pages = (len(gs) + PAGE - 1) // PAGE
        page = max(0, min(page, pages - 1))
        rows = [[(("✅ " if g["added"] else "➕ ") + g["title"][:40] + (" [форум]" if g.get("forum") else ""), f"gt:{i}")]
                for i, g in enumerate(gs[page * PAGE:(page + 1) * PAGE], page * PAGE)]
        if pages > 1:
            rows.append([("◀️", f"gp:{max(0, page - 1)}"), (f"{page + 1}/{pages}", f"gp:{page}"),
                         ("▶️", f"gp:{min(pages - 1, page + 1)}")])
        rows.append([("🔍 Поиск", "gs"), ("✔️ Готово", "ch")])
        await self.show(uid, f"➕ Твои группы ({len(gs)}). Нажимай, чтобы добавить в рассылку или убрать (✅ — уже в ней).",
                        kb(*rows), mid)

    async def toggle_group(self, uid, i, mid):
        gs = self.cache.get(uid, {}).get("groups") or []
        if not 0 <= i < len(gs):
            return await self.groups_screen(uid, 0, mid)
        g = gs[i]
        res, err = await self.do(uid, "del" if g["added"] else "add", ids=[g["id"]])
        if err:
            return await self.send(uid, "⚠️ " + ERRORS.get(err, err))
        if res.get("bad"):
            await self.send(uid, "⚠️ " + "\n".join(res["bad"]))
        else:
            g["added"] = not g["added"]
        await self.groups_screen(uid, i // PAGE, mid, refresh=False)

    async def remove_screen(self, uid, page, mid=None):
        chats = self.cache.get(uid, {}).get("chats")
        if chats is None:
            return await self.chats_screen(uid, mid)
        if not chats:
            return await self.chats_screen(uid, mid)
        pages = (len(chats) + PAGE - 1) // PAGE
        page = max(0, min(page, pages - 1))
        rows = [[("🗑 " + c["title"][:45], f"rd:{i}")] for i, c in enumerate(chats[page * PAGE:(page + 1) * PAGE], page * PAGE)]
        if pages > 1:
            rows.append([("◀️", f"rm:{max(0, page - 1)}"), (f"{page + 1}/{pages}", f"rm:{page}"),
                         ("▶️", f"rm:{min(pages - 1, page + 1)}")])
        rows.append([("✔️ Готово", "ch")])
        await self.show(uid, "🗑 Какой чат убрать из рассылки?", kb(*rows), mid)

    async def remove_chat(self, uid, i, mid):
        chats = self.cache.get(uid, {}).get("chats") or []
        if 0 <= i < len(chats):
            res, err = await self.do(uid, "del", keys=[chats[i]["key"]])
            if err:
                return await self.send(uid, "⚠️ " + ERRORS.get(err, err))
            chats.pop(i)
        await self.remove_screen(uid, i // PAGE, mid)

    def _add_note(self, res, err):
        if err:
            return "⚠️ " + ERRORS.get(err, err)
        out = []
        if res.get("added"):
            out.append("✅ Добавлено: " + ", ".join(res["added"]))
        if res.get("bad"):
            out.append("⚠️ " + "\n⚠️ ".join(res["bad"]))
        return "\n".join(out) or "Эти чаты уже в рассылке."

    async def interval_screen(self, uid, mid=None):
        inf = self.info(self.db.user(uid) or {})
        cur = (inf.get("stats") or {}).get("interval")
        rows = [[(lbl, f"is:{sec}") for sec, lbl in INTERVALS[i:i + 3]] for i in range(0, len(INTERVALS), 3)]
        rows.append([("✏️ Своё", "ic"), ("↩️ Меню", "m")])
        await self.show(uid, "🕐 Как часто отправлять в каждый чат?" + (f"\nСейчас: каждые {fmt_dur(cur)}." if cur else "") +
                        "\n\nСоветую от 30 минут: чаще — быстрее банят в чатах и ограничивает Telegram.", kb(*rows), mid)

    async def set_interval(self, uid, sec, mid):
        res, err = await self.do(uid, "interval", sec=sec)
        await self.home(uid, mid, note=("⚠️ " + ERRORS.get(err, err)) if err else
                        f"✅ Интервал: каждые {fmt_dur(res['interval'])}" +
                        (" (минимум — 5 мин)" if res["interval"] > sec else ""))

    async def btn_simple(self, uid, action, mid):
        res, err = await self.do(uid, action, timeout=60)
        if err == "not_connected":
            return await self.welcome(uid, mid)
        if err or not res.get("ok"):
            return await self.home(uid, mid, note="⚠️ " + ERRORS.get(err or res.get("error"), err or res.get("error")))
        if action == "now":
            u = self.db.user(uid)
            self.db.upsert(uid, next_ping=min(u.get("next_ping") or 0, time.time() + min(PING_EVERY, res["count"] * 8 + 20)))
            return await self.home(uid, mid, note=f"🚀 Рассылаю в {res['count']} чат(ов) — отчёт пришлю сюда.")
        if action == "test":
            await self.send(uid, "📨 Пример отправлен тебе в «Избранное». Вот как увидят в чатах:")
            await self.preview(uid, res["preview"])
            return await self.home(uid)
        await self.home(uid, mid, note="▶️ Рассылка включена. Первые сообщения уйдут в течение минуты." if action == "on"
                        else "⏸ Рассылка выключена.")

    async def ask_support(self, uid):
        self.steps[uid] = {"step": "support"}
        await self.send(uid, "Напиши вопрос одним сообщением — передам владельцу бота. /menu — отмена.")

    # ── подключение сервера ──
    def _valid_url(self, url):
        if self.allow_http and re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+", url):
            return True
        return bool(re.fullmatch(r"https://(?=[^/]*[a-z])[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?", url)) and \
            not re.search(r"localhost|\.local$|\.internal$", url)

    async def on_connect(self, m, uid, url, key):
        await self.q("deleteMessage", chat_id=uid, message_id=m["message_id"])
        url = url.rstrip("/").lower()
        now = time.time()
        tl = [t for t in self.connect_times.get(uid, []) if now - t < 600]
        if len(tl) >= 6:
            return await self.send(uid, "Слишком много попыток подключения. Подожди 10 минут.")
        self.connect_times[uid] = tl + [now]
        if not self._valid_url(url):
            return await self.send(uid, "⚠️ Странный адрес сервера — подключение отклонено.")
        code, res = await self.node_call(url, "/api/ping", {"footer": self.footer}, key, 60)
        if not (code == 200 and isinstance(res, dict) and res.get("ok")):
            return await self.send(uid, "⚠️ Не достучался до твоего сервера. Обнови его страницу и войди ещё раз.")
        if res.get("owner") != uid or res.get("state") != "running":
            return await self.send(uid, "⚠️ Сервер вошёл не в этот аккаунт Telegram — подключение отклонено.")
        others = self.db.rows("SELECT uid, last_ok FROM users WHERE url=? AND uid!=?", (url, uid))
        if any(now - (o["last_ok"] or 0) < 7 * 86400 for o in others):
            return await self.send(uid, "⚠️ Этот сервер уже подключён к другому аккаунту Telegram. Создай свой сервер: "
                                        "/start → 🔌 Подключить.")
        for o in others:
            self.db.upsert(o["uid"], url=None, key=None, blob=None, ver=None, info=None)
            await self.send(o["uid"], "⚠️ Твой сервер теперь подключён к другому аккаунту Telegram — тут он отключён.")
        prev = self.db.user(uid) or {}
        st = res.get("stats") or {}
        self.db.upsert(uid, url=url, key=key, blob=res.get("blob"), ver=res.get("ver"), last_ok=now, fails=0,
                       next_ping=now + PING_EVERY, alert="", ack=0, sent_prev=int(st.get("sent_total") or 0),
                       info=json.dumps({"state": "running", "me": res.get("me"), "stats": st, "v": res.get("v")},
                                       ensure_ascii=False))
        me = res.get("me") or {}
        old = f"\nСтарый сервер ({prev['url']}) больше не нужен — можешь удалить его в Render." \
            if prev.get("url") and prev["url"] != url else ""
        await self.send(uid, f"✅ Метроном подключён! Аккаунт: {me.get('name', '')}"
                             + (f" (@{me['username']})" if me.get("username") else "") + old +
                        "\n\nДальше 3 шага: 📝 Реклама → 💬 Чаты → ▶️ Включить.")
        await self.home(uid)

    # ── магазин ──
    def reach(self):
        now = time.time()
        rows = self.db.rows("SELECT * FROM users WHERE url IS NOT NULL AND url != ''")
        alive = [u for u in rows if now - (u.get("last_ok") or 0) < 20 * 60]
        working = [u for u in alive if (self.info(u).get("stats") or {}).get("enabled")]
        chats = sum((self.info(u).get("stats") or {}).get("on", 0) for u in working)
        t = self.today()
        y = int(self.db.get("sent:" + (t - timedelta(days=1)).isoformat(), 0))
        w = sum(int(self.db.get("sent:" + (t - timedelta(days=i)).isoformat(), 0)) for i in range(1, 8))
        td = int(self.db.get("sent:" + t.isoformat(), 0))
        return {"users": len(rows), "alive": len(alive), "working": len(working), "chats": chats, "yesterday": y,
                "week": w, "today": td}

    async def shop_screen(self, uid, mid=None):
        r, s = self.reach(), self.shop
        start = s.free_from()
        live = s.live()
        text = ("💰 Реклама в приписке Метронома\n\n"
                "Под каждым сообщением, которое рассылают пользователи Метронома, стоит приписка — и в ней может быть "
                "твоя строка:\n\n…сообщение пользователя…\n" + self.base_footer() + "\n" + AD_PREFIX + "твой текст со ссылкой\n\n"
                f"Сейчас работает рассылок: {r['working']} · чатов в них: {r['chats']}\n"
                f"Отправлено сообщений: вчера {r['yesterday']} · за 7 дней {r['week']}\n\n"
                f"Цена: {s.st['price']} ⭐ за сутки. Одна реклама за раз — ни с кем не делишь место.\n"
                "Ближайший старт: " + ("сегодня, сразу после оплаты" if start == self.today() else dd(start)) +
                ("\n\nТвоя реклама сейчас в эфире до " + dd(date.fromisoformat(live["end"])) if live and live["user_id"] == uid else ""))
        await self.show(uid, text, kb([("🛒 Купить рекламу", "buy")], [("📋 Мои заказы", "my"), ("↩️ Назад", "start")]), mid)

    # ── владелец ──
    async def adm_claim(self, uid, arg):
        if self.admin == uid:
            return await self.adm_help()
        code = (self.cfg.get("ADMIN_CODE") or "").strip()
        if not self.admin and code and arg.strip() == code:
            self.admin = self.shop.admin = uid
            self.db.set("admin", uid)
            try:
                with open(os.path.join(HERE, "config.env"), "a", encoding="utf-8") as f:
                    f.write(f"\nADMIN_ID={uid}\n")
            except OSError:
                pass
            await self.send(uid, "👑 Ты владелец бота.")
            if await self.restore_backup():
                await self.send(uid, "♻️ База восстановлена из закреплённой копии.")
            return await self.adm_help()
        await self.home(uid)

    async def adm_help(self, arg=None, m=None):
        await self.send(self.admin, "👑 Команды владельца\n\n"
                                    "/stats — пользователи, рассылки, охват, деньги\n"
                                    "/broadcast — разослать сообщение всем пользователям бота\n"
                                    "/footer текст — своя основная строка приписки · /footer reset — стандартная\n"
                                    "/backup — прислать копию базы сейчас\n\n"
                                    "Реклама в приписке:\n"
                                    "/queue — заявки и очередь · /price 50 — цена за сутки в ⭐\n"
                                    "/add 3 — поставить рекламу на 3 суток без оплаты (продал за рубли)\n"
                                    "/reject 5 причина · /drop 5 — снять · /refund 5 — снять и вернуть ⭐\n"
                                    "/close · /open — пауза продаж / открыть\n"
                                    "/reply 123 текст — ответить человеку")

    adm_admin = adm_help

    async def adm_stats(self, arg, m=None):
        r = self.reach()
        total = len(self.db.rows("SELECT uid FROM users"))
        await self.send(self.admin, f"📊 Пользователей бота: {total}\nПодключено серверов: {r['users']} · на связи: {r['alive']}\n"
                                    f"Рассылка включена: {r['working']} · чатов в рассылке: {r['chats']}\n"
                                    f"Сообщений с припиской: сегодня {r['today']} · вчера {r['yesterday']} · за 7 дней {r['week']}\n\n"
                                    f"Реклама: заработано {self.shop.st['earned']} ⭐ (≈ ${self.shop.st['earned'] * 0.013:.0f}) · "
                                    f"свободно с {dd(self.shop.free_from())}\n"
                                    f"Приписка сейчас:\n{self.footer['text']}")

    async def adm_footer(self, arg, m=None):
        if arg.lower() == "reset":
            self.db.set("base_footer", None)
        elif arg:
            if len(arg) > 200:
                return await self.send(self.admin, "Длинно — до 200 символов.")
            self.db.set("base_footer", arg)
        self.update_footer()
        await self.send(self.admin, "Приписка (уйдёт на все серверы за 5 минут):\n\n" + self.footer["text"])

    async def adm_broadcast(self, arg, m=None):
        self.steps[self.admin] = {"step": "broadcast"}
        await self.send(self.admin, "Пришли сообщение для рассылки всем пользователям бота (можно с фото). /menu — отмена.")

    async def broadcast(self, m):
        ids = [u["uid"] for u in self.db.rows("SELECT uid FROM users") if u["uid"] != self.admin]
        ok = 0
        for uid in ids:
            if await self.q("copyMessage", chat_id=uid, from_chat_id=self.admin, message_id=m["message_id"]):
                ok += 1
            await asyncio.sleep(0.05)
        await self.send(self.admin, f"📣 Отправлено {ok} из {len(ids)}.")

    async def adm_backup(self, arg=None, m=None):
        await self.backup(force=True)

    # ── бэкап базы ──
    async def backup(self, force=False):
        if not self.admin:
            return
        last = float(self.db.get("last_backup", 0))
        if not force and time.time() - last < 86400:
            return
        data = self.db.dump()
        data["shop"] = self.shop.st
        raw = json.dumps(data, ensure_ascii=False).encode()
        try:
            msg = await self.api("sendDocument", chat_id=self.admin, disable_notification=True,
                                 caption=f"{BACKUP_TAG} — копия базы бота. Не пересылай никому: тут ключи управления серверами.",
                                 _document=("metronom_hub_backup.json", raw))
        except Exception as e:
            log.warning("бэкап: %s", e)
            return
        old = self.db.get("backup_msg")
        self.db.set("backup_msg", msg["message_id"])
        self.db.set("last_backup", time.time())
        await self.q("pinChatMessage", chat_id=self.admin, message_id=msg["message_id"], disable_notification=True)
        if old:
            await self.q("unpinChatMessage", chat_id=self.admin, message_id=old)

    async def restore_backup(self):
        if not self.admin or self.db.rows("SELECT uid FROM users WHERE url IS NOT NULL AND url != '' LIMIT 1"):
            return False
        chat = await self.q("getChat", chat_id=self.admin) or {}
        pm = chat.get("pinned_message") or {}
        if not (pm.get("document") and (pm.get("caption") or "").startswith(BACKUP_TAG)):
            return False
        try:
            data = json.loads(await self.api.download(pm["document"]["file_id"]))
        except Exception as e:
            log.warning("не смог восстановить бэкап: %s", e)
            return False
        self.db.load(data)
        if data.get("shop"):
            self.shop.st.update(data["shop"])
            self.shop.save()
        self.footer = self.db.get("footer")
        log.info("база восстановлена из бэкапа: %d пользователей", len(data.get("users", [])))
        return True

    async def ticker(self):
        while True:
            try:
                await self.shop.tick()
                await self.backup()
            except Exception:
                log.exception("тик")
            await asyncio.sleep(60)


# ───────────────────────── запуск ─────────────────────────

async def amain():
    cfg = load_env(os.path.join(HERE, "config.env"))
    for k in ("BOT_TOKEN", "REPO", "ADMIN_CODE", "ADMIN_ID", "PRICE"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if not cfg.get("BOT_TOKEN"):
        log.error("Нет BOT_TOKEN в config.env")
        return 1
    api = BotAPI(cfg["BOT_TOKEN"].strip())
    try:
        me = await api("getMe")
    except ApiError as e:
        log.error("Токен не подходит: %s", e.desc)
        return 2
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(8))
    cfg["BOT_USERNAME"] = me["username"]
    db_url = (os.environ.get("DB_URL") or cfg.get("DB_URL") or "").strip()
    try:
        db = DB(db_url or os.path.join(HERE, "hub.db"), os.environ.get("DB_TOKEN") or cfg.get("DB_TOKEN"))
    except Exception as e:
        log.error("База не открылась: %s", e)
        return 3
    log.info("база: %s", "облако Turso" if db_url else "файл hub.db")
    hub = Hub(api, db, cfg, os.path.join(HERE, "ads.json"))
    if await hub.restore_backup():
        await hub.send(hub.admin, "♻️ База восстановлена из закреплённой копии.")
    await hub.q("deleteWebhook")
    await hub.q("setMyCommands", commands=[
        {"command": "menu", "description": "Моя рассылка"}, {"command": "start", "description": "Начало"},
        {"command": "buy", "description": "Купить рекламу в приписке"}, {"command": "help", "description": "Как это работает"},
        {"command": "support", "description": "Вопрос владельцу"}, {"command": "paysupport", "description": "Вопрос по оплате"}])
    log.info("бот @%s запущен · REPO=%s · админ: %s", me["username"], hub.repo or "НЕ ЗАДАН", hub.admin or "не назначен")
    if not hub.admin and cfg.get("ADMIN_CODE"):
        log.info("Назначь себя владельцем: напиши боту  /admin %s", cfg["ADMIN_CODE"])
    asyncio.ensure_future(hub.pinger())
    asyncio.ensure_future(hub.ticker())
    offset, tasks = 0, set()
    while True:
        try:
            ups = await api("getUpdates", offset=offset, timeout=50,
                            allowed_updates=["message", "callback_query", "pre_checkout_query"])
        except ApiError as e:
            if e.code == 401:
                log.error("Токен отозван — возьми новый в @BotFather.")
                return 2
            log.warning("getUpdates: %s", e.desc)
            await asyncio.sleep(10)
            continue
        except Exception as e:
            log.warning("сеть: %s", e)
            await asyncio.sleep(5)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            t = asyncio.ensure_future(hub.handle(u))
            tasks.add(t)
            t.add_done_callback(lambda _t: (tasks.discard(_t), _t.exception() and log.error("update: %r", _t.exception())))


if __name__ == "__main__":
    ads_bot.setup_logging()
    sys.exit(asyncio.run(amain()) or 0)
