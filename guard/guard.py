"""Метроном · Страж — бот-модератор для пиар-чатов.

Обязательная подписка (ОП) с продажей мест посуточно за звёзды, кулдаун, капча, антиспам и фильтры,
бан/мут/варны, объявления по расписанию, закреп и VIP за звёзды, статистика. Управление — панель
в личке бота, вход только по паролю.

Только стандартный Python 3.9+. Настройки — config.env рядом или переменные окружения: BOT_TOKEN, PASSWORD, TZ
(или TZ_NAME), NEW_PASSWORD (сброс пароля), DB_URL + DB_TOKEN (облачная база Turso).
"""
import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:          # pragma: no cover
    ZoneInfo = None

VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.append(_p)
try:
    import tursodb                      # облачная база Turso (metronom/tursodb.py), без библиотек
except ImportError:                     # pragma: no cover
    tursodb = None
log = logging.getLogger("guard")

TICK = 3                 # как часто тикер проверяет таймеры, сек
SUB_CACHE = 600          # сколько помнить «подписан», сек
NOTICE_GAP = 60          # одно и то же предупреждение одному человеку — не чаще, сек
CHAT_NOTICE_GAP = 2      # любые предупреждения в одном чате — не чаще, сек
LOGIN_TRIES = (5, 3600)  # 5 неверных паролей за час — блок на час
PAGE = 8
DAYS_ALL = (1, 3, 7, 14, 30)
UPDATES = ["message", "edited_message", "callback_query", "my_chat_member", "chat_member", "pre_checkout_query"]
NO_PERMS = {k: False for k in (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos",
    "can_send_video_notes", "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
    "can_add_web_page_previews", "can_change_info", "can_invite_users", "can_pin_messages", "can_manage_topics")}
ALL_PERMS = {k: True for k in NO_PERMS}
MEDIA = [("photo", "фото"), ("video", "видео"), ("animation", "GIF"), ("sticker", "стикеры"), ("voice", "голосовые"),
         ("video_note", "кружки"), ("audio", "музыка"), ("document", "файлы"), ("poll", "опросы"),
         ("contact", "контакты"), ("location", "геолокация"), ("dice", "кубики/игры")]
CAPTCHA = [("🍎", "яблоко"), ("🍌", "банан"), ("🚗", "машину"), ("🐶", "собаку"), ("⚽", "мяч"), ("🌙", "луну"),
           ("🎁", "подарок"), ("🔑", "ключ"), ("🐱", "кошку"), ("🍕", "пиццу")]
TZS = [("Europe/Kaliningrad", "Калининград +2"), ("Europe/Moscow", "Москва +3"), ("Europe/Samara", "Самара +4"),
       ("Asia/Yekaterinburg", "Екатеринбург +5"), ("Asia/Omsk", "Омск +6"), ("Asia/Novosibirsk", "Новосибирск +7"),
       ("Asia/Irkutsk", "Иркутск +8"), ("Asia/Vladivostok", "Владивосток +10"), ("Europe/Kyiv", "Киев"),
       ("Europe/Minsk", "Минск +3"), ("Asia/Almaty", "Алматы +5"), ("Asia/Tashkent", "Ташкент +5")]
TZ_FALLBACK = {"Europe/Kaliningrad": 2, "Europe/Moscow": 3, "Europe/Samara": 4, "Asia/Yekaterinburg": 5, "Asia/Omsk": 6,
               "Asia/Novosibirsk": 7, "Asia/Irkutsk": 8, "Asia/Vladivostok": 10, "Europe/Kyiv": 2, "Europe/Minsk": 3,
               "Asia/Almaty": 5, "Asia/Tashkent": 5}
KIND_NAME = {"op": "Канал в ОП", "pin": "Закреп", "vip": "VIP"}
STATUS_NAME = {"draft": "черновик", "review": "на проверке", "approved": "ждёт оплаты", "paid": "оплачен, в очереди",
               "live": "в эфире", "done": "завершён", "rejected": "отклонён", "refunded": "возврат", "cancelled": "отменён"}


# ───────────────────────── мелочи ─────────────────────────

def esc(s):
    return html.escape(str(s or ""), quote=False)


def mention(uid, name):
    return f'<a href="tg://user?id={int(uid)}">{esc(name or uid)}</a>'


def u16(s):
    return len(s.encode("utf-16-le")) // 2


def uname(u):
    if not u:
        return "?"
    n = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("title") or ""
    return n or ("@" + u["username"] if u.get("username") else str(u.get("id")))


def fmt_dur(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return "навсегда"
    if sec < 60:
        return f"{sec} с"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    out = ([f"{d} д"] if d else []) + ([f"{h} ч"] if h else []) + ([f"{m} мин"] if m and not d else [])
    return " ".join(out) or "1 мин"


def fmt_left(sec):
    return fmt_dur(max(60, int(sec))) if sec > 59 else f"{max(1, int(sec))} с"


_DU = {"s": 1, "с": 1, "сек": 1, "m": 60, "м": 60, "мин": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400,
       "w": 604800, "н": 604800}


def parse_dur(s):
    """30m · 2h · 1d · 1h30m · 90 (минут) · 0 / навсегда → секунды (0 = навсегда), None — не понял."""
    s = (s or "").strip().lower().replace(" ", "")
    if s in ("0", "навсегда", "forever", "perm", "∞"):
        return 0
    if s.isdigit():
        return int(s) * 60
    total, pos = 0, 0
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)(сек|мин|s|с|m|м|h|ч|d|д|w|н)", s):
        if m.start() != pos:
            return None
        total += float(m.group(1).replace(",", ".")) * _DU[m.group(2)]
        pos = m.end()
    return int(total) if s and pos == len(s) and total > 0 else None


def get_tz(name):
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone(timedelta(hours=TZ_FALLBACK.get(name, 5)))


def dn(n):
    n = int(n)
    w = "сутки" if n % 10 == 1 and n % 100 != 11 else "суток"
    return f"{n} {w}"


def stars(n):
    return f"{int(n)} ⭐"


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def kb(*rows):
    out = []
    for row in rows:
        r = []
        for b in row:
            if isinstance(b, dict):
                r.append(b)
            else:
                t, c = b
                r.append({"text": t, "url": c[4:]} if c.startswith("url:") else {"text": t, "callback_data": c})
        if r:
            out.append(r)
    return {"inline_keyboard": out}


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()


def check_pw(pw, stored):
    try:
        salt, _ = stored.split("$", 1)
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def msg_link(chat_id, mid, username=None):
    if username:
        return f"https://t.me/{username}/{mid}"
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{mid}" if s.startswith("-100") else ""


def subst(text, ents, mapping):
    """Подставить {name}, {chat}… в текст с разметкой, сдвигая смещения. mapping: ключ → (текст, entity|None)."""
    ents = [dict(e) for e in ents or []]
    if not mapping:
        return text, ents
    pat = re.compile(r"\{(" + "|".join(re.escape(k) for k in mapping) + r")\}")
    out, extra, pts, i = "", [], [], 0
    for m in pat.finditer(text):
        out += text[i:m.start()]
        rep, ent = mapping[m.group(1)]
        if ent:
            extra.append(dict(ent, offset=u16(out), length=u16(rep)))
        pts.append((u16(text[:m.start()]), u16(m.group(0)), u16(rep)))
        out += rep
        i = m.end()
    out += text[i:]

    def mp(x, end):
        d = 0
        for o, lo, lr in pts:
            if o + lo <= x:
                d += lr - lo
            elif o < x < o + lo:
                return o + d + (lr if end else 0)
        return x + d
    for e in ents:
        a, b = mp(e["offset"], False), mp(e["offset"] + e["length"], True)
        e["offset"], e["length"] = a, max(0, b - a)
    return out, [e for e in ents if e["length"] > 0] + extra


_URL = re.compile(r"(?:https?://|www\.)[^\s]+|(?<![\w@/])t\.me/[^\s]+|(?<![\w@/.])@[A-Za-z][A-Za-z0-9_]{3,31}\b", re.I)
_SCRIPTS = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF\u0E00-\u0E7F]")


def entity_text(text, e):
    b = text.encode("utf-16-le")
    return b[e["offset"] * 2:(e["offset"] + e["length"]) * 2].decode("utf-16-le", "ignore")


def links_of(m):
    """Все ссылки и @упоминания сообщения → список строк (нижний регистр)."""
    text = m.get("text") or m.get("caption") or ""
    ents = m.get("entities") or m.get("caption_entities") or []
    out = []
    for e in ents:
        if e["type"] in ("url", "mention"):
            out.append(entity_text(text, e).lower())
        elif e["type"] == "text_link":
            out.append(e.get("url", "").lower())
    for b in (m.get("reply_markup") or {}).get("inline_keyboard", []):
        for x in b:
            if x.get("url"):
                out.append(x["url"].lower())
    out += [x.lower() for x in _URL.findall(text)]
    return list(dict.fromkeys(out))


def domain_of(link):
    if link.startswith("@"):
        return "t.me"
    link = re.sub(r"^[a-z]+://", "", link)
    d = link.split("/")[0].split("?")[0].split(":")[0]
    return d[4:] if d.startswith("www.") else d


def is_tg_link(link):
    return link.startswith("@") or domain_of(link) in ("t.me", "telegram.me", "telegram.dog") or link.startswith("tg://")


def media_kind(m):
    for k, _ in MEDIA:
        if k in m:
            return k
    if "game" in m:
        return "dice"
    if "venue" in m:
        return "location"
    return None


def content_from(m):
    """Сообщение → то, что можно переслать ботом позже (текст/фото/видео/GIF/файл)."""
    if m.get("text"):
        return {"type": "text", "text": m["text"], "ents": m.get("entities") or []}
    for k in ("photo", "video", "animation", "document"):
        if k in m:
            fid = m[k][-1]["file_id"] if k == "photo" else m[k]["file_id"]
            return {"type": k, "file_id": fid, "text": m.get("caption") or "", "ents": m.get("caption_entities") or []}
    return None


def parse_buttons(text):
    """Строки «Текст - https://ссылка» → [[{text,url}], …]; None — ошибка."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        row = []
        for part in line.split("||"):
            m = re.match(r"^(.+?)\s+[-—|]\s+((?:https?://|tg://|t\.me/)\S+)$", part.strip())
            if not m:
                return None
            url = m.group(2)
            if url.startswith("t.me/"):
                url = "https://" + url
            row.append({"text": m.group(1)[:60], "url": url})
        rows.append(row)
    return rows or None


def load_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


# ───────────────────────── Bot API без библиотек ─────────────────────────

class ApiError(Exception):
    def __init__(self, desc, code=None, params=None):
        super().__init__(desc)
        self.desc, self.code, self.params = desc or "", code, params or {}


class BotAPI:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.files = f"https://api.telegram.org/file/bot{token}/"

    def _post(self, method, body, ctype, timeout):
        req = urllib.request.Request(self.base + method, body, {"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "error_code": e.code, "description": str(e)}

    async def __call__(self, method, **p):
        doc = p.pop("_document", None)
        timeout = p.get("timeout", 0) + 25
        for attempt in range(3):
            if doc:
                body, ctype = self._multipart(p, doc)
            else:
                body, ctype = json.dumps(p, ensure_ascii=False).encode(), "application/json"
            res = await asyncio.to_thread(self._post, method, body, ctype, timeout)
            if res.get("ok"):
                return res["result"]
            wait = (res.get("parameters") or {}).get("retry_after")
            if res.get("error_code") == 429 and wait and wait <= 30 and attempt < 2:
                await asyncio.sleep(wait + 0.5)
                continue
            raise ApiError(res.get("description"), res.get("error_code"), res.get("parameters"))

    @staticmethod
    def _multipart(p, doc):
        name, data = doc
        b = uuid.uuid4().hex
        out = io.BytesIO()
        for k, v in p.items():
            v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool)) else str(v)
            out.write(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        out.write(f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                  f'Content-Type: application/json\r\n\r\n'.encode() + data + f"\r\n--{b}--\r\n".encode())
        return out.getvalue(), f"multipart/form-data; boundary={b}"

    async def download(self, file_id):
        f = await self("getFile", file_id=file_id)

        def get():
            with urllib.request.urlopen(self.files + f["file_path"], timeout=30) as r:
                return r.read()
        return await asyncio.to_thread(get)


# ───────────────────────── база ─────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, title TEXT, username TEXT, s TEXT, added REAL, active INTEGER DEFAULT 1,
  rights TEXT DEFAULT '{}', night TEXT, members INTEGER DEFAULT 0, last_msg REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT, first REAL, last REAL);
CREATE INDEX IF NOT EXISTS users_un ON users(username);
CREATE TABLE IF NOT EXISTS members(chat INTEGER, uid INTEGER, msgs INTEGER DEFAULT 0, last_post REAL DEFAULT 0,
  joined REAL DEFAULT 0, last REAL DEFAULT 0, PRIMARY KEY(chat, uid));
CREATE TABLE IF NOT EXISTS warns(id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, uid INTEGER, reason TEXT, by INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS gbans(uid INTEGER PRIMARY KEY, reason TEXT, by INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat INTEGER, uid INTEGER, action TEXT,
  reason TEXT, by INTEGER);
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, title TEXT, username TEXT, link TEXT, kind TEXT, chats TEXT DEFAULT '"all"',
  active INTEGER DEFAULT 1, until REAL, order_id INTEGER, added REAL, broken TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS op_joins(channel INTEGER, uid INTEGER, ts REAL, left REAL, PRIMARY KEY(channel, uid));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, uid INTEGER, uname TEXT, data TEXT DEFAULT '{}',
  days INTEGER, price INTEGER, status TEXT, start REAL, end REAL, charge TEXT, created REAL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS vip(uid INTEGER PRIMARY KEY, until REAL, note TEXT);
CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, content TEXT, s TEXT, next REAL DEFAULT 0,
  last REAL DEFAULT 0, sent TEXT DEFAULT '{}', created REAL);
CREATE TABLE IF NOT EXISTS captcha(chat INTEGER, uid INTEGER, mid INTEGER, deadline REAL, answer TEXT, PRIMARY KEY(chat, uid));
CREATE TABLE IF NOT EXISTS trash(chat INTEGER, mid INTEGER, at REAL);
CREATE TABLE IF NOT EXISTS stats(day TEXT, chat INTEGER, k TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(day, chat, k));
CREATE TABLE IF NOT EXISTS staff(uid INTEGER PRIMARY KEY, role TEXT, name TEXT, since REAL);
"""
TABLES = ("kv", "chats", "users", "members", "warns", "gbans", "log", "channels", "op_joins", "orders", "vip", "posts",
          "captcha", "trash", "stats", "staff")


class DB:
    def __init__(self, path, token=None):
        remote = bool(tursodb and tursodb.is_remote(path))
        if remote:
            self.c = tursodb.Connection(path, token or "")
        elif "://" in str(path):
            raise RuntimeError("Для облачной базы нужен файл metronom/tursodb.py")
        else:
            self.c = sqlite3.connect(path, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        if path != ":memory:" and not remote:
            self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)

    def q(self, sql, *a):
        return [dict(r) for r in self.c.execute(sql, a).fetchall()]

    def one(self, sql, *a):
        r = self.c.execute(sql, a).fetchone()
        return dict(r) if r else None

    def x(self, sql, *a):
        return self.c.execute(sql, a)

    def get(self, k, default=None):
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v, ensure_ascii=False)))

    def stat(self, day, chat, k, n=1):
        self.c.execute("INSERT INTO stats(day, chat, k, n) VALUES(?,?,?,?) ON CONFLICT(day, chat, k) DO UPDATE SET n=n+?",
                       (day, chat, k, n, n))

    def dump(self):
        return {t: self.q(f"SELECT * FROM {t}") for t in TABLES}

    def load(self, d):
        self.c.execute("BEGIN")
        try:
            for t in TABLES:
                if t not in d:
                    continue
                self.c.execute(f"DELETE FROM {t}")
                for row in d[t]:
                    cols = list(row)
                    self.c.execute(f"INSERT INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                   [row[k] for k in cols])
            self.c.execute("COMMIT")
        except Exception:
            self.c.execute("ROLLBACK")
            raise


# ───────────────────────── схема настроек (одна на всю админку) ─────────────────────────

def _dur_opts(vals, zero="выкл"):
    return [(v, fmt_dur(v) if v else zero) for v in vals]


FIELDS = {}


def F(key, kind, label, default, opts=None, hint="", custom=None):
    FIELDS[key] = {"key": key, "kind": kind, "label": label, "default": default, "opts": opts, "hint": hint,
                   "custom": custom}


# чат
F("op", "bool", "Обязательная подписка", True, hint="Кто не подписан на каналы из списка ОП — сообщение удаляется, "
  "бот показывает кнопки «Подписаться». Каналы — в главном меню → 📢 ОП-каналы.")
F("op_mode", "choice", "Если не подписан", "delete", [("delete", "удалять сообщения"), ("mute", "удалять + мут 10 мин (снимет кнопка «Я подписался»)")])
F("cooldown", "choice", "Кулдаун", 0, _dur_opts([0, 60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]),
  "Одному человеку — одно сообщение в N. Админы, помощники и VIP не ограничены.", "dur")
F("cd_mode", "choice", "Что считать", "all", [("all", "все сообщения"),
                                               ("ads", "только рекламу (ссылки, @, медиа, от 150 символов)")])
F("captcha", "bool", "Капча для новичков", True, hint="Новичок не может писать, пока не нажмёт нужную кнопку. "
  "Не успел или ошибся — бот выкидывает (зайти заново можно).")
F("captcha_time", "choice", "Время на капчу", 120, [(60, "1 мин"), (120, "2 мин"), (300, "5 мин"), (600, "10 мин")])
F("newbie_h", "choice", "Новичкам без ссылок и медиа", 0, [(0, "выкл"), (1, "1 ч"), (6, "6 ч"), (24, "24 ч"), (72, "3 дня")])
F("bots", "bool", "Выкидывать ботов, добавленных не админами", True)
F("welcome_on", "bool", "Приветствие", False)
F("welcome", "text", "Текст приветствия", {"text": "👋 {name}, добро пожаловать в {chat}!\nПравила — /rules", "ents": []},
  hint="Пришли сообщение с любым оформлением. Подстановки: {name} — имя (ссылкой), {chat} — название чата.")
F("welcome_del", "choice", "Удалять приветствие через", 120, [(0, "не удалять"), (30, "30 с"), (60, "1 мин"), (120, "2 мин"),
                                                              (300, "5 мин")])
F("rules", "text", "Правила (/rules)", {"text": "", "ents": []}, hint="Пусто — бот сам соберёт правила из настроек чата.")
F("links", "choice", "Ссылки", "allow", [("allow", "разрешены все"), ("tg", "только Telegram (t.me, @)"),
                                          ("white", "только из белого списка"), ("none", "запрещены")])
F("link_white", "list", "Белый список доменов", [], hint="Домены через запятую: youtube.com, vk.com. t.me пиши тоже, если нужен.")
F("forwards", "choice", "Пересылки", "all", [("all", "разрешены"), ("users", "только от людей, не из каналов"),
                                             ("none", "запрещены")])
F("as_channel", "bool", "Писать от имени канала", False, hint="Выкл — сообщения от имени каналов удаляются, канал банится в чате.")
F("maxlen", "choice", "Макс. длина сообщения", 0, [(0, "без лимита"), (200, "200"), (500, "500"), (1000, "1000"),
                                                  (2000, "2000"), (3000, "3000")], custom="int")
F("scripts", "bool", "Удалять арабскую вязь и иероглифы", True, hint="Частый спам от ботов.")
F("media", "multi", "Разрешённые типы сообщений", {k: k != "poll" for k, _ in MEDIA}, MEDIA)
F("stop", "list", "Стоп-слова", [], hint="Слова или куски слов через запятую. Регистр не важен, ё = е.")
F("stop_action", "choice", "За стоп-слово", "warn", [("delete", "удалить"), ("warn", "удалить + варн"),
                                                     ("mute", "удалить + мут на сутки"), ("ban", "удалить + бан")])
F("flood_on", "bool", "Антифлуд", True)
F("flood_n", "choice", "Сообщений подряд", 5, [(3, "3"), (4, "4"), (5, "5"), (7, "7"), (10, "10")])
F("flood_sec", "choice", "За время", 10, [(5, "5 с"), (10, "10 с"), (30, "30 с"), (60, "1 мин")])
F("flood_mute", "choice", "Мут за флуд", 600, _dur_opts([300, 600, 1800, 3600, 86400]), custom="dur")
F("warn_limit", "choice", "Варнов до наказания", 3, [(2, "2"), (3, "3"), (4, "4"), (5, "5")])
F("warn_action", "choice", "Наказание", "mute", [("mute", "мут"), ("kick", "кик"), ("ban", "бан")])
F("warn_mute", "choice", "Срок мута / бана", 86400, _dur_opts([3600, 21600, 86400, 259200, 604800, 0], "навсегда"),
  custom="dur")
F("warn_days", "choice", "Варны сгорают через", 30, [(7, "7 дней"), (30, "30 дней"), (90, "90 дней"), (0, "никогда")])
F("night_on", "bool", "Ночной режим", False, hint="Ночью писать могут только админы. Утром бот откроет чат сам.")
F("night_from", "choice", "Закрывать в", 1, [(h, f"{h:02d}:00") for h in range(24)])
F("night_to", "choice", "Открывать в", 7, [(h, f"{h:02d}:00") for h in range(24)])
F("service", "bool", "Удалять «вошёл / вышел»", True)
F("notice_del", "choice", "Удалять ответы бота через", 30, [(15, "15 с"), (30, "30 с"), (60, "1 мин"), (120, "2 мин")])
F("admin_cmds", "bool", "Админы чата могут модерировать командами", True)
# общие
F("shop_open", "bool", "Продажа рекламы открыта", True)
F("approve", "bool", "Одобрять заявки вручную", True, hint="Выкл — счёт приходит покупателю сразу (кроме проверки канала).")
F("days", "multi", "Сроки на выбор", {"1": True, "3": True, "7": True, "14": False, "30": False},
  [(str(d), dn(d)) for d in DAYS_ALL])
F("op_sale", "bool", "Продавать места в ОП", True)
F("op_price", "choice", "Цена места в ОП за сутки", 100, [(v, stars(v)) for v in (25, 50, 75, 100, 150, 200, 300, 500)],
  custom="int")
F("op_slots", "choice", "Платных мест в ОП одновременно", 2, [(i, str(i)) for i in range(1, 6)])
F("pin_sale", "bool", "Продавать закреп", True)
F("pin_price", "choice", "Цена закрепа за сутки", 150, [(v, stars(v)) for v in (50, 100, 150, 200, 300, 500, 1000)],
  custom="int")
F("pin_slots", "choice", "Закрепов одновременно", 1, [(1, "1"), (2, "2"), (3, "3")])
F("pin_loud", "bool", "Закреплять с уведомлением всем", False)
F("vip_sale", "bool", "Продавать VIP", True, hint="VIP пишет без кулдауна, без ОП и без ограничений на ссылки и длину.")
F("vip_price", "choice", "Цена VIP за сутки", 30, [(v, stars(v)) for v in (10, 15, 20, 30, 50, 75, 100)], custom="int")
F("tz", "choice", "Часовой пояс", "Asia/Yekaterinburg", TZS)
F("leave_foreign", "bool", "Выходить из чужих чатов", True, hint="Если бота добавит кто-то кроме владельца — бот выйдет.")
F("reports_pm", "bool", "Жалобы /report — мне в личку", True)
F("backup_daily", "bool", "Бэкап базы раз в сутки", True)
# объявление
F("p_on", "bool", "Включено", True)
F("p_mode", "choice", "Расписание", "every", [("every", "каждые N"), ("times", "в указанное время")])
F("p_every", "choice", "Интервал", 3600, _dur_opts([900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]), custom="dur")
F("p_times", "times", "Время отправки", ["12:00"], hint="Пришли время через запятую: 10:00, 15:30, 21:00")
F("p_chats", "chats", "Чаты", "all")
F("p_pin", "bool", "Закреплять", False)
F("p_del", "bool", "Удалять прошлую отправку", True, hint="Чтобы в чате висело только свежее объявление.")
F("p_silent", "bool", "Без звука", True)
F("p_act", "bool", "Только если в чате писали", True, hint="Не слать в мёртвый чат: если с прошлой отправки никто не писал — пропуск.")
F("p_btns", "btns", "Кнопки-ссылки", [], hint="Каждая кнопка с новой строки: «Текст - https://ссылка». "
  "Две кнопки в ряд — через ||")

CHAT_KEYS = ["op", "op_mode", "cooldown", "cd_mode", "captcha", "captcha_time", "newbie_h", "bots", "welcome_on", "welcome",
             "welcome_del", "rules", "links", "link_white", "forwards", "as_channel", "maxlen", "scripts", "media", "stop",
             "stop_action", "flood_on", "flood_n", "flood_sec", "flood_mute", "warn_limit", "warn_action", "warn_mute",
             "warn_days", "night_on", "night_from", "night_to", "service", "notice_del", "admin_cmds"]
GLOBAL_KEYS = ["shop_open", "approve", "days", "op_sale", "op_price", "op_slots", "pin_sale", "pin_price", "pin_slots",
               "pin_loud", "vip_sale", "vip_price", "tz", "leave_foreign", "reports_pm", "backup_daily"]
POST_KEYS = ["p_on", "p_mode", "p_every", "p_times", "p_chats", "p_pin", "p_del", "p_silent", "p_act", "p_btns"]
SECTIONS = {
    "c": [("op", "🔐 Подписка (ОП)", ["op", "op_mode"]), ("cd", "⏱ Кулдаун", ["cooldown", "cd_mode"]),
          ("cap", "🤖 Капча и новички", ["captcha", "captcha_time", "newbie_h", "bots"]),
          ("wel", "👋 Приветствие и правила", ["welcome_on", "welcome", "welcome_del", "rules"]),
          ("flt", "🧹 Фильтры", ["links", "link_white", "forwards", "as_channel", "maxlen", "scripts", "media", "stop",
                               "stop_action"]),
          ("fl", "🌊 Антифлуд", ["flood_on", "flood_n", "flood_sec", "flood_mute"]),
          ("wr", "⚠️ Варны", ["warn_limit", "warn_action", "warn_mute", "warn_days"]),
          ("nt", "🌙 Ночной режим", ["night_on", "night_from", "night_to"]),
          ("etc", "⚙️ Прочее", ["service", "notice_del", "admin_cmds"])],
    "g": [("shop", "💰 Магазин", ["shop_open", "approve", "days"]),
          ("sop", "📢 Места в ОП", ["op_sale", "op_price", "op_slots"]),
          ("spin", "📌 Закреп", ["pin_sale", "pin_price", "pin_slots", "pin_loud"]),
          ("svip", "⭐ VIP", ["vip_sale", "vip_price"]),
          ("gen", "⚙️ Общие", ["tz", "leave_foreign", "reports_pm", "backup_daily"])],
    "p": [("post", "🗓 Объявление", POST_KEYS)],
}


def defaults(keys):
    return {k: json.loads(json.dumps(FIELDS[k]["default"])) for k in keys}


def merged(keys, stored):
    d = defaults(keys)
    for k, v in (stored or {}).items():
        if k in d:
            if FIELDS[k]["kind"] == "multi" and isinstance(v, dict):
                d[k].update(v)
            else:
                d[k] = v
    return d


def show_value(f, v):
    k = f["kind"]
    if k == "bool":
        return "✅ вкл" if v else "▫️ выкл"
    if k == "choice":
        for ov, lbl in f["opts"]:
            if ov == v:
                return lbl
        return fmt_dur(v) if f["custom"] == "dur" else (stars(v) if "price" in f["key"] else str(v))
    if k == "text":
        t = (v or {}).get("text") or ""
        return ("«" + t[:40].replace("\n", " ") + ("…»" if len(t) > 40 else "»")) if t else "не задан"
    if k in ("list", "times"):
        return ", ".join(v[:6]) + (f" и ещё {len(v) - 6}" if len(v) > 6 else "") if v else "пусто"
    if k == "multi":
        on = [lbl for sk, lbl in f["opts"] if v.get(sk)]
        return ", ".join(on) if on else "ничего"
    if k == "chats":
        return "все" if v == "all" else plural(len(v), "чат", "чата", "чатов")
    if k == "btns":
        return plural(sum(len(r) for r in v), "кнопка", "кнопки", "кнопок") if v else "нет"
    return str(v)


# ───────────────────────── бот ─────────────────────────

MEDIA_NAME = dict(MEDIA)
RIGHTS = [("can_delete_messages", "удалять сообщения"), ("can_restrict_members", "банить"),
          ("can_pin_messages", "закреплять"), ("can_invite_users", "приглашать")]
CONTENT_KEYS = ("text", "caption", "game", "venue", "story", "paid_media") + tuple(k for k, _ in MEDIA)
MOD_CMDS = {"ban", "unban", "mute", "unmute", "kick", "warn", "unwarn", "warns", "del", "purge", "pin", "unpin", "ro", "cd",
            "info", "id", "help", "vip", "unvip", "gban", "ungban", "setlog"}
STAFF_CMDS = {"vip", "unvip", "gban", "ungban", "setlog"}
HELP_GROUP = """<b>Команды модерации</b> (ответом на сообщение или с @ником / ID):
/ban [срок] [причина] — бан (без срока — навсегда)
/unban — разбан · /kick — выкинуть (сможет зайти)
/mute [срок] [причина] — мут (по умолчанию 1 ч) · /unmute
/warn [причина] — варн, на лимите наказание · /unwarn · /warns
/del — удалить сообщение · /purge — удалить всё от этого сообщения до команды
/pin [loud] — закрепить · /unpin — открепить
/ro [срок] — закрыть чат (без срока — до /ro) · /ro off — открыть
/cd [срок] — кулдаун в этом чате (0 — выкл)
/info — карточка человека · /id — ID чата и человека
Только владелец и помощники: /vip [дней] · /unvip · /gban · /ungban · /setlog
Срок: 30m, 2h, 1d, 1h30m, 0 — навсегда. Ответом на сообщение /ban, /kick и /warn удаляют и его."""


BUYER_CB = {"shop", "buy", "bd", "osub", "my", "mo", "ocx", "nobtn"}
BUYER_STEPS = {"ord_ch", "ord_post", "ord_btn"}
HELPER_CB = {"m", "noop", "mod", "mh", "mf", "u", "um", "umd", "uu", "ub", "uub", "uk", "uw", "uv", "uvd", "jl", "gb", "vl",
             "oq", "ol", "o", "oa", "orj", "orjn", "opv", "st", "lo"}


def missing_rights(r):
    if not r.get("admin"):
        return ["бот не админ"]
    return [lbl for k, lbl in RIGHTS if not r.get(k)]


def is_content(m):
    return any(k in m for k in CONTENT_KEYS)


def inside(x):
    return x["status"] in ("creator", "administrator", "member") or (x["status"] == "restricted" and x.get("is_member"))


def dur_reason(args, default):
    if args:
        d = parse_dur(args[0])
        if d is not None:
            return d, " ".join(args[1:])
    return default, " ".join(args)



class Guard:
    def __init__(self, api, db, env=None, clock=time.time):
        self.api, self.db, self.env, self.now = api, db, env or {}, clock
        self.me, self.me_id, self.username = {}, 0, "bot"
        self.cfg = merged(GLOBAL_KEYS, db.get("cfg"))
        self.tz = get_tz(self.cfg["tz"])
        self.steps = {}
        self.admins = {}
        self.subcache = {}
        self.flood = {}
        self.notice_t = {}
        self.chat_notice_t = {}
        self.op_missing = {}
        self.recent_join = {}
        self.login_fail = {}
        self.locks = {}
        self.albums = {}
        self.flood_mg = {}
        self.last_welcome = {}
        self.recent_left = {}
        self.op_muted = set()
        self.marks = {"min": 0, "hour": 0, "day": None}
        if not db.get("pw") and self.env.get("PASSWORD"):
            db.set("pw", hash_pw(self.env["PASSWORD"].strip()))

    # ── время ──
    def local(self, ts=None):
        return datetime.fromtimestamp(self.now() if ts is None else ts, self.tz)

    def day(self, ts=None):
        return self.local(ts).date().isoformat()

    def dt(self, ts):
        d = self.local(ts)
        return d.strftime("%d.%m %H:%M")

    # ── хранение ──
    def save_cfg(self):
        self.db.set("cfg", self.cfg)
        self.tz = get_tz(self.cfg["tz"])

    def chat(self, cid):
        r = self.db.one("SELECT * FROM chats WHERE id=?", cid)
        if r:
            r["s"] = merged(CHAT_KEYS, json.loads(r["s"] or "{}"))
            r["rights"] = json.loads(r["rights"] or "{}")
        return r

    def chats(self, active=True):
        rows = self.db.q("SELECT id FROM chats" + (" WHERE active=1" if active else "") + " ORDER BY added")
        return [self.chat(r["id"]) for r in rows]

    def save_chat_s(self, cid, s):
        self.db.x("UPDATE chats SET s=? WHERE id=?", json.dumps(s, ensure_ascii=False), cid)

    def post(self, pid):
        r = self.db.one("SELECT * FROM posts WHERE id=?", pid)
        if r:
            r["s"] = merged(POST_KEYS, json.loads(r["s"] or "{}"))
            r["content"] = json.loads(r["content"] or "null")
            r["sent"] = json.loads(r["sent"] or "{}")
        return r

    def order(self, oid):
        r = self.db.one("SELECT * FROM orders WHERE id=?", oid)
        if r:
            r["data"] = json.loads(r["data"] or "{}")
        return r

    def save_order(self, o, **f):
        o.update(f)
        cols = {k: (json.dumps(o["data"], ensure_ascii=False) if k == "data" else o[k]) for k in
                ("kind", "uid", "uname", "data", "days", "price", "status", "start", "end", "charge", "note")}
        self.db.x(f"UPDATE orders SET {', '.join(k + '=?' for k in cols)} WHERE id=?", *cols.values(), o["id"])

    def seen(self, u):
        if not u or u.get("is_bot"):
            return
        now = self.now()
        self.db.x("INSERT INTO users(id, name, username, first, last) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                  "name=excluded.name, username=excluded.username, last=excluded.last",
                  u["id"], uname(u), (u.get("username") or "").lower() or None, now, now)

    def user(self, uid):
        return self.db.one("SELECT * FROM users WHERE id=?", uid)

    def find_user(self, ref):
        ref = (ref or "").strip()
        if re.fullmatch(r"-?\d+", ref):
            return self.user(int(ref)) or {"id": int(ref), "name": ref, "username": None}
        m = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{3,31})", ref)
        if m:
            return self.db.one("SELECT * FROM users WHERE username=?", m.group(1).lower())
        return None

    def who(self, uid):
        u = self.user(uid)
        return mention(uid, (u or {}).get("name") or uid)

    def stat(self, cid, k, n=1):
        self.db.stat(self.day(), cid, k, n)

    # ── доступ ──
    def role(self, uid):
        r = self.db.one("SELECT role FROM staff WHERE uid=?", uid)
        return r["role"] if r else None

    def is_owner(self, uid):
        return self.role(uid) == "owner"

    def staff(self, role=None):
        return [r["uid"] for r in self.db.q("SELECT uid FROM staff" + (" WHERE role=?" if role else ""),
                                            *([role] if role else []))]

    def is_vip(self, uid):
        r = self.db.one("SELECT until FROM vip WHERE uid=?", uid)
        return bool(r and (r["until"] == 0 or r["until"] > self.now()))

    async def chat_admins(self, cid, fresh=False):
        a = self.admins.get(cid)
        if a and not fresh and self.now() - a[1] < 600:
            return a[0]
        try:
            lst = await self.api("getChatAdministrators", chat_id=cid)
            ids = {x["user"]["id"] for x in lst}
        except ApiError:
            ids = a[0] if a else set()
        self.admins[cid] = (ids, self.now())
        return ids

    async def privileged(self, c, uid):
        if not uid:
            return False
        if self.role(uid):
            return True
        return uid in await self.chat_admins(c["id"])

    async def can_moderate(self, c, uid):
        if self.role(uid):
            return True
        return c["s"]["admin_cmds"] and uid in await self.chat_admins(c["id"])

    # ── отправка ──
    async def q(self, method, **p):
        try:
            return await self.api(method, **p)
        except ApiError as e:
            log.info("%s: %s", method, e.desc)
        except Exception as e:
            log.warning("%s: %r", method, e)
        return None

    async def send(self, chat_id, text, markup=None, **p):
        p.setdefault("parse_mode", "HTML")
        p.setdefault("link_preview_options", {"is_disabled": True})
        if markup:
            p["reply_markup"] = markup
        return await self.q("sendMessage", chat_id=chat_id, text=text[:4096], **p)

    async def to_owners(self, text, markup=None):
        for uid in self.staff("owner"):
            await self.send(uid, text, markup)

    async def to_staff(self, text, markup=None):
        for uid in self.staff():
            await self.send(uid, text, markup)

    def trash(self, cid, mid, ttl):
        if mid and ttl:
            self.db.x("INSERT INTO trash(chat, mid, at) VALUES(?,?,?)", cid, mid, self.now() + ttl)

    async def notice(self, c, uid, kind, text, markup=None, ttl=None, force=False):
        """Короткое сообщение в чат, само удаляется. Не спамит: одному человеку одно и то же — раз в минуту."""
        now, cid = self.now(), c["id"]
        k = (cid, uid, kind)
        if not force:
            if now - self.notice_t.get(k, 0) < NOTICE_GAP or now - self.chat_notice_t.get(cid, 0) < CHAT_NOTICE_GAP:
                return None
        self.notice_t[k] = self.chat_notice_t[cid] = now
        m = await self.send(cid, text, markup, disable_notification=True)
        if m:
            self.trash(cid, m["message_id"], ttl or c["s"]["notice_del"])
        return m

    async def delete(self, cid, mid):
        return await self.q("deleteMessage", chat_id=cid, message_id=mid)

    async def act_log(self, cid, uid, action, reason="", by=None):
        self.db.x("INSERT INTO log(ts, chat, uid, action, reason, by) VALUES(?,?,?,?,?,?)", self.now(), cid, uid, action,
                  reason or "", by)
        lc = self.db.get("log_chat")
        if lc:
            c = self.chat(cid) if cid else None
            await self.send(lc, f"{action} · {esc(c['title']) if c else 'все чаты'} · {self.who(uid)} <code>{uid}</code>"
                                + (f"\nПричина: {esc(reason)}" if reason else "") + (f"\nКто: {self.who(by)}" if by else ""),
                            disable_notification=True)

    # ── наказания ──
    async def mute(self, cid, uid, sec, reason="", by=None, quiet=False):
        until = int(self.now() + sec) if sec else 0
        r = await self.q("restrictChatMember", chat_id=cid, user_id=uid, permissions=NO_PERMS,
                         use_independent_chat_permissions=True, until_date=until)
        if r and not quiet:
            await self.act_log(cid, uid, f"🔇 Мут {fmt_dur(sec)}", reason, by)
        return bool(r)

    async def unmute(self, cid, uid, by=None, quiet=False):
        r = await self.q("restrictChatMember", chat_id=cid, user_id=uid, permissions=ALL_PERMS,
                         use_independent_chat_permissions=True)
        if r and not quiet:
            await self.act_log(cid, uid, "🔊 Размут", "", by)
        return bool(r)

    async def ban(self, cid, uid, sec=0, reason="", by=None, quiet=False):
        until = int(self.now() + sec) if sec else 0
        r = await self.q("banChatMember", chat_id=cid, user_id=uid, until_date=until, revoke_messages=False)
        if r and not quiet:
            await self.act_log(cid, uid, f"⛔ Бан {fmt_dur(sec)}", reason, by)
        return bool(r)

    async def unban(self, cid, uid, by=None, quiet=False):
        r = await self.q("unbanChatMember", chat_id=cid, user_id=uid, only_if_banned=True)
        if r and not quiet:
            await self.act_log(cid, uid, "✅ Разбан", "", by)
        return bool(r)

    async def kick(self, cid, uid, reason="", by=None, quiet=False):
        r = await self.q("banChatMember", chat_id=cid, user_id=uid, until_date=int(self.now() + 60))
        await self.q("unbanChatMember", chat_id=cid, user_id=uid, only_if_banned=True)
        if r and not quiet:
            await self.act_log(cid, uid, "👢 Кик", reason, by)
        return bool(r)

    def warns(self, c, uid):
        days = c["s"]["warn_days"]
        since = self.now() - days * 86400 if days else 0
        return self.db.q("SELECT * FROM warns WHERE chat=? AND uid=? AND ts>=? ORDER BY ts", c["id"], uid, since)

    async def warn(self, c, uid, reason="", by=None):
        """Варн; на лимите — наказание. → (число варнов, текст наказания или '')."""
        self.db.x("INSERT INTO warns(chat, uid, reason, by, ts) VALUES(?,?,?,?,?)", c["id"], uid, reason, by, self.now())
        await self.act_log(c["id"], uid, "⚠️ Варн", reason, by)
        n, s = len(self.warns(c, uid)), c["s"]
        if n < s["warn_limit"]:
            return n, ""
        self.db.x("DELETE FROM warns WHERE chat=? AND uid=?", c["id"], uid)
        why = f"{n} варна" if n < 5 else f"{n} варнов"
        if s["warn_action"] == "ban":
            await self.ban(c["id"], uid, s["warn_mute"], why, by)
            return n, f"⛔ бан {fmt_dur(s['warn_mute'])}"
        if s["warn_action"] == "kick":
            await self.kick(c["id"], uid, why, by)
            return n, "👢 кик"
        await self.mute(c["id"], uid, s["warn_mute"], why, by)
        return n, f"🔇 мут {fmt_dur(s['warn_mute'])}"

    # ── участники ──
    def touch(self, cid, uid, **f):
        self.db.x("INSERT INTO members(chat, uid) VALUES(?, ?) ON CONFLICT DO NOTHING", cid, uid)
        if f:
            self.db.x(f"UPDATE members SET {', '.join(k + '=?' for k in f)} WHERE chat=? AND uid=?", *f.values(), cid, uid)

    def member(self, cid, uid):
        return self.db.one("SELECT * FROM members WHERE chat=? AND uid=?", cid, uid) or \
            {"chat": cid, "uid": uid, "msgs": 0, "last_post": 0, "joined": 0, "last": 0}

    def count_msg(self, c, uid):
        now = self.now()
        self.touch(c["id"], uid)
        self.db.x("UPDATE members SET msgs=msgs+1, last=? WHERE chat=? AND uid=?", now, c["id"], uid)
        self.db.x("UPDATE chats SET last_msg=? WHERE id=?", now, c["id"])
        self.stat(c["id"], "msgs")

    def gbanned(self, uid):
        return bool(self.db.one("SELECT 1 FROM gbans WHERE uid=?", uid))

    # ── входящие ──
    async def on_update(self, up):
        try:
            if "message" in up:
                await self.on_message(up["message"])
            elif "edited_message" in up:
                await self.on_message(up["edited_message"], edited=True)
            elif "callback_query" in up:
                await self.on_callback(up["callback_query"])
            elif "my_chat_member" in up:
                await self.on_my_member(up["my_chat_member"])
            elif "chat_member" in up:
                await self.on_member(up["chat_member"])
            elif "pre_checkout_query" in up:
                await self.on_precheckout(up["pre_checkout_query"])
        except Exception:
            log.exception("update %s", up.get("update_id"))

    async def on_message(self, m, edited=False):
        t = m["chat"]["type"]
        if t == "private":
            if not edited:
                await self.on_private(m)
        elif t in ("group", "supergroup"):
            await self.on_group(m, edited)

    async def on_group(self, m, edited=False):
        cid = m["chat"]["id"]
        if m.get("migrate_to_chat_id"):
            return self.migrate(cid, m["migrate_to_chat_id"])
        u = m.get("from") or {}
        uid = u.get("id")
        c = self.chat(cid)
        if not c:
            if self.is_owner(uid) and (m.get("text") or "").startswith("/"):
                await self.register_chat(m["chat"], u)        # бота добавили, пока он был выключен
            return
        if not c["active"]:
            return
        s = c["s"]
        if m.get("new_chat_title"):
            self.db.x("UPDATE chats SET title=? WHERE id=?", m["new_chat_title"], cid)
        if "new_chat_members" in m or "left_chat_member" in m:
            for nu in m.get("new_chat_members") or []:
                if nu["id"] != self.me_id:
                    await self.on_join(c, nu, u if u.get("id") != nu["id"] else None)
            lu = m.get("left_chat_member")
            if lu and lu["id"] != self.me_id:
                await self.on_left(c, lu)
            if s["service"]:
                await self.delete(cid, m["message_id"])
            return
        if "pinned_message" in m:
            if uid == self.me_id:
                await self.delete(cid, m["message_id"])     # «бот закрепил сообщение» — мусор
            return
        if not is_content(m) or m.get("is_automatic_forward") or uid == self.me_id:
            return
        mid = m["message_id"]
        sc = m.get("sender_chat")
        if sc:
            if sc["id"] in (cid, c["rights"].get("linked")):  # анонимный админ или свой привязанный канал
                if sc["id"] == cid and (m.get("text") or "").startswith("/") and not edited:
                    await self.group_cmd(c, m)
                return
            if not s["as_channel"]:
                await self.delete(cid, mid)
                self.stat(cid, "del_flt")
                await self.q("banChatSenderChat", chat_id=cid, sender_chat_id=sc["id"])
                await self.notice(c, sc["id"], "chan", "🚫 Писать от имени канала здесь нельзя — пиши от своего аккаунта.")
                return
            uid = sc["id"]
        else:
            self.seen(u)
        text = m.get("text") or m.get("caption") or ""
        if text.startswith("/") and not edited and not sc:
            if await self.group_cmd(c, m):
                return
        if not sc and await self.privileged(c, uid):
            if not edited:
                self.count_msg(c, uid)
            return
        if self.gbanned(uid):
            await self.delete(cid, mid)
            await self.ban(cid, uid, 0, "глобальный бан", quiet=True)
            return
        if self.db.one("SELECT 1 FROM captcha WHERE chat=? AND uid=?", cid, uid):
            await self.delete(cid, mid)
            return
        vip = not sc and self.is_vip(uid)
        name = esc(sc["title"]) if sc else mention(uid, uname(u))
        if not edited and not sc and s["flood_on"] and await self.flood_check(c, uid, m, name):
            return
        bad = self.check(c, m, uid, vip, text)
        if bad:
            code, why = bad
            await self.delete(cid, mid)
            self.stat(cid, "del_stop" if code == "stop" else "del_flt")
            if code == "stop" and s["stop_action"] != "delete" and not sc:
                await self.punish_stop(c, uid, name)
            else:
                await self.notice(c, uid, code, f"🧹 {name}, {why}.")
            return
        if edited:
            return
        mg = m.get("media_group_id")
        k = (cid, uid)
        if mg:                                              # альбом = одно сообщение
            prev = self.albums.get(k)
            if prev and prev[0] == mg:
                if prev[1]:
                    self.stat(cid, "msgs")
                else:
                    await self.delete(cid, mid)
                return
        if s["op"] and not vip and not sc:
            missing = await self.op_check(c, uid)
            if missing:
                if mg:
                    self.albums[k] = (mg, False)
                await self.delete(cid, mid)
                self.stat(cid, "del_op")
                self.op_missing[uid] = (self.now(), {ch["id"] for ch in missing})
                extra, ttl = "", None
                if s["op_mode"] == "mute" and await self.mute(cid, uid, 600, "нет подписки", quiet=True):
                    self.op_muted.add(k)
                    extra, ttl = "\nДо подписки — мут на 10 минут. Подпишись и нажми «✅ Я подписался».", 600
                rows = [[{"text": f"📢 {ch['title'][:40]}", "url": ch["link"]}] for ch in missing if ch["link"]]
                rows.append([{"text": "✅ Я подписался", "callback_data": f"sub:{uid}"}])
                await self.notice(c, uid, "op", f"🔐 {name}, чтобы писать в этом чате, подпишись:" + extra,
                                  {"inline_keyboard": rows}, ttl=ttl, force=bool(extra))
                return
        cd = s["cooldown"]
        if cd and not vip and (s["cd_mode"] == "all" or self.is_ad(m, text)):
            left = self.member(cid, uid)["last_post"] + cd - self.now()
            if left > 0:
                if mg:
                    self.albums[k] = (mg, False)
                await self.delete(cid, mid)
                self.stat(cid, "del_cd")
                await self.notice(c, uid, "cd", f"⏱ {name}, следующее сообщение — через {fmt_left(left)}.")
                return
            self.touch(cid, uid, last_post=self.now())
        if mg:
            self.albums[k] = (mg, True)
        self.count_msg(c, uid)

    @staticmethod
    def is_ad(m, text):
        return bool(links_of(m) or media_kind(m) or len(text) >= 150)

    def own_link(self, c, link):
        names = {self.username.lower(), (c.get("username") or "").lower()}
        for ch in self.db.q("SELECT username, link FROM channels"):
            names.add((ch["username"] or "").lower())
            if ch["link"] and ch["link"].lower().split("://")[-1] in link:
                return True
        names.discard("")
        m = re.match(r"^(?:@|(?:https?://)?(?:t|telegram)\.me/)([a-z0-9_]+)", link)
        return bool(m and m.group(1) in names)

    @staticmethod
    def white(s, link):
        d = domain_of(link)
        for w in s["link_white"]:
            w = domain_of(w.lower().strip())
            if w and (d == w or d.endswith("." + w)):
                return True
        return False

    def check(self, c, m, uid, vip, text):
        """Фильтры содержания → (код, почему) или None."""
        s = c["s"]
        if s["scripts"] and _SCRIPTS.search(text):
            return "scripts", "такие символы здесь запрещены"
        low = text.lower().replace("ё", "е")
        for w in s["stop"]:
            w = w.lower().replace("ё", "е").strip()
            if w and w in low:
                return "stop", "запрещённое слово"
        k = media_kind(m)
        if k and not s["media"].get(k, True):
            return "media", f"{MEDIA_NAME[k]} здесь нельзя"
        fo = m.get("forward_origin")
        if not fo and ("forward_from_chat" in m or "forward_from" in m or "forward_sender_name" in m):
            fo = {"type": "channel" if "forward_from_chat" in m else "user"}
        if fo:
            if s["forwards"] == "none":
                return "fwd", "пересылать сообщения здесь нельзя"
            if s["forwards"] == "users" and fo.get("type") in ("channel", "chat"):
                return "fwd", "пересылки из каналов здесь нельзя"
        if vip:
            return None
        links = [x for x in links_of(m) if not self.own_link(c, x)]
        mode = s["links"]
        if links and mode != "allow":
            if mode == "none":
                bad = links
            elif mode == "tg":
                bad = [x for x in links if not is_tg_link(x)]
            else:
                bad = [x for x in links if not self.white(s, x)]
            if bad:
                return "links", {"none": "ссылки здесь запрещены", "tg": "здесь можно только ссылки на Telegram",
                                 "white": "такие ссылки здесь запрещены"}[mode]
        if s["maxlen"] and len(text) > s["maxlen"]:
            return "len", f"слишком длинно — максимум {s['maxlen']} символов"
        if s["newbie_h"] and (links or k):
            j = self.member(c["id"], uid)["joined"]
            left = j + s["newbie_h"] * 3600 - self.now() if j else 0
            if left > 0:
                return "newbie", f"ссылки и медиа новичкам можно через {fmt_left(left)}"
        return None

    async def flood_check(self, c, uid, m, name):
        s, now, k = c["s"], self.now(), (c["id"], uid)
        mg = m.get("media_group_id")
        if mg and self.flood_mg.get(k) == mg:
            return False
        if mg:
            self.flood_mg[k] = mg
        q = [t for t in self.flood.get(k, []) if now - t < s["flood_sec"]] + [now]
        self.flood[k] = q
        if len(q) < s["flood_n"]:
            return False
        self.flood.pop(k, None)
        await self.delete(c["id"], m["message_id"])
        self.stat(c["id"], "del_flood")
        await self.mute(c["id"], uid, s["flood_mute"], "флуд")
        await self.notice(c, uid, "flood", f"🌊 {name} — мут {fmt_dur(s['flood_mute'])} за флуд.", force=True)
        return True

    async def punish_stop(self, c, uid, name):
        s, cid = c["s"], c["id"]
        act = s["stop_action"]
        if act == "warn":
            n, pun = await self.warn(c, uid, "стоп-слово")
            txt = f"⚠️ {name}, запрещённое слово. Варн {n}/{s['warn_limit']}" + (f" → {pun}." if pun else ".")
        elif act == "mute":
            await self.mute(cid, uid, 86400, "стоп-слово")
            txt = f"🔇 {name} — мут на сутки за запрещённое слово."
        else:
            await self.ban(cid, uid, 0, "стоп-слово")
            txt = f"⛔ {name} — бан за запрещённое слово."
        await self.notice(c, uid, "stop", txt, force=True)

    # ── обязательная подписка ──
    def op_channels(self, cid=None):
        out, now = [], self.now()
        for r in self.db.q("SELECT * FROM channels WHERE active=1 AND broken='' ORDER BY kind DESC, added"):
            if r["kind"] == "paid" and r["until"] and r["until"] <= now:
                continue
            chats = json.loads(r["chats"] or '"all"')
            if cid is None or chats == "all" or cid in chats:
                out.append(r)
        return out

    async def subscribed(self, ch, uid, fresh=False):
        """True / False / None (не удалось проверить — пропускаем, чтобы не ломать чат)."""
        k = (ch["id"], uid)
        t = self.subcache.get(k)
        if t and not fresh and self.now() - t < SUB_CACHE:
            return True
        try:
            m = await self.api("getChatMember", chat_id=ch["id"], user_id=uid)
        except ApiError as e:
            d = e.desc.lower()
            if "user not found" in d or "participant_id_invalid" in d or "not a member" in d and "bot" not in d:
                return False
            await self.channel_broken(ch, e.desc)
            return None
        except Exception:
            return None
        ok = inside(m)
        if ok:
            self.subcache[k] = self.now()
        else:
            self.subcache.pop(k, None)
        return ok

    async def op_check(self, c, uid, fresh=False):
        missing = []
        was = self.op_missing.get(uid)
        for ch in self.op_channels(c["id"]):
            ok = await self.subscribed(ch, uid, fresh)
            if ok is False:
                missing.append(ch)
            elif ok and was and ch["id"] in was[1] and self.now() - was[0] < 3600:
                was[1].discard(ch["id"])
                self.op_joined(ch["id"], uid)
        return missing

    def op_joined(self, ch_id, uid):
        cur = self.db.x("INSERT INTO op_joins(channel, uid, ts, left) VALUES(?,?,?,NULL) ON CONFLICT(channel, uid) "
                        "DO UPDATE SET ts=excluded.ts, left=NULL WHERE left IS NOT NULL", ch_id, uid, self.now())
        if cur.rowcount:
            self.stat(ch_id, "op_join")

    async def channel_broken(self, ch, why):
        if ch.get("broken"):
            return
        ch["broken"] = why or "ошибка"
        self.db.x("UPDATE channels SET broken=? WHERE id=?", ch["broken"], ch["id"])
        await self.to_owners(f"⚠️ Канал <b>{esc(ch['title'])}</b> временно убран из ОП: бот не может проверить подписку "
                             f"({esc(why)}).\nВерни бота админом в канал — он снова включится сам.",
                             kb([("📢 Открыть канал в панели", f"oc:{ch['id']}")]))
        if ch["kind"] == "paid" and ch.get("order_id"):
            o = self.order(ch["order_id"])
            if o:
                await self.send(o["uid"], f"⚠️ Заказ #{o['id']}: бот потерял админку в канале «{esc(ch['title'])}», "
                                          "поэтому канал сейчас не в ОП. Верни бота админом — всё продолжится.")

    async def cb_op(self, cq, uid):
        msg = cq["message"]
        c = self.chat(msg["chat"]["id"])
        if cq["from"]["id"] != uid:
            return "Эта кнопка для другого человека 🙂"
        if not c:
            return ""
        missing = await self.op_check(c, uid, fresh=True)
        if missing:
            return {"text": "Ещё нет подписки на: " + ", ".join(ch["title"] for ch in missing)[:180], "show_alert": True}
        await self.delete(c["id"], msg["message_id"])
        if (c["id"], uid) in self.op_muted:
            self.op_muted.discard((c["id"], uid))
            await self.unmute(c["id"], uid, quiet=True)
        return "✅ Спасибо! Теперь можно писать."

    # ── вход / выход людей ──
    async def on_join(self, c, u, by=None):
        cid, uid, now, s = c["id"], u["id"], self.now(), c["s"]
        k = (cid, uid)
        if now - self.recent_join.get(k, 0) < 60:
            return
        self.recent_join[k] = now
        self.recent_left.pop(k, None)
        if u.get("is_bot"):
            if s["bots"] and not (by and await self.privileged(c, by["id"])):
                if await self.ban(cid, uid, 0, quiet=True):
                    await self.act_log(cid, uid, "🤖 Бот выкинут", "добавлен не админом", by and by["id"])
            return
        self.seen(u)
        self.touch(cid, uid, joined=now)
        self.stat(cid, "join")
        if self.gbanned(uid):
            await self.ban(cid, uid, 0, "глобальный бан", quiet=True)
            return
        if s["captcha"] and not self.is_vip(uid) and not await self.privileged(c, uid) and \
                not (by and await self.privileged(c, by["id"])):
            if await self.captcha_start(c, u):
                return
        await self.welcome(c, u)

    async def on_left(self, c, u):
        k, now = (c["id"], u["id"]), self.now()
        if now - self.recent_left.get(k, 0) < 60:
            return
        self.recent_left[k] = now
        self.recent_join.pop(k, None)
        self.stat(c["id"], "left")
        row = self.db.one("SELECT * FROM captcha WHERE chat=? AND uid=?", c["id"], u["id"])
        if row:
            self.db.x("DELETE FROM captcha WHERE chat=? AND uid=?", c["id"], u["id"])
            if row["mid"]:
                await self.delete(c["id"], row["mid"])

    async def captcha_start(self, c, u):
        cid, uid, t = c["id"], u["id"], c["s"]["captcha_time"]
        if not await self.mute(cid, uid, 0, quiet=True):
            return False
        opts = random.sample(range(len(CAPTCHA)), 4)
        ans = random.choice(opts)
        msg = await self.send(cid, f"👋 {mention(uid, uname(u))}, привет! Чтобы писать в чате, нажми на "
                                   f"<b>{CAPTCHA[ans][1]}</b>. Время — {fmt_dur(t)}.",
                              kb([(CAPTCHA[i][0], f"cap:{uid}:{i}") for i in opts],
                                 [("✅ Пустить (админ)", f"cap:{uid}:ok"), ("⛔ Бан (админ)", f"cap:{uid}:ban")]),
                              disable_notification=True)
        self.db.x("INSERT OR REPLACE INTO captcha(chat, uid, mid, deadline, answer) VALUES(?,?,?,?,?)", cid, uid,
                  msg and msg["message_id"], self.now() + t, str(ans))
        return True

    async def cb_captcha(self, cq, parts):
        msg = cq["message"]
        cid, uid, a, who = msg["chat"]["id"], int(parts[1]), parts[2], cq["from"]["id"]
        c = self.chat(cid)
        row = self.db.one("SELECT * FROM captcha WHERE chat=? AND uid=?", cid, uid)
        if not c or not row:
            await self.delete(cid, msg["message_id"])
            return "Капча уже неактуальна"
        if a in ("ok", "ban"):
            if not await self.privileged(c, who):
                return "Эта кнопка для админов"
            await self.captcha_done(c, row, a == "ok", by=who)
            return "Готово"
        if who != uid:
            return "Это капча для другого человека 🙂"
        ok = a == row["answer"]
        await self.captcha_done(c, row, ok)
        return "✅ Добро пожаловать!" if ok else {"text": "❌ Неверно. Можно зайти в чат заново и попробовать ещё раз.",
                                                 "show_alert": True}

    async def captcha_done(self, c, row, ok, by=None):
        cid, uid = c["id"], row["uid"]
        self.db.x("DELETE FROM captcha WHERE chat=? AND uid=?", cid, uid)
        if row["mid"]:
            await self.delete(cid, row["mid"])
        if ok:
            await self.unmute(cid, uid, quiet=True)
            self.stat(cid, "captcha_ok")
            await self.welcome(c, {"id": uid, "first_name": (self.user(uid) or {}).get("name") or str(uid)})
        elif by:
            await self.ban(cid, uid, 0, "бан с капчи", by)
        else:
            self.stat(cid, "captcha_fail")
            await self.kick(cid, uid, "не прошёл капчу", quiet=True)

    async def welcome(self, c, u):
        s = c["s"]
        if not s["welcome_on"] or not s["welcome"]["text"]:
            return
        name = uname(u)
        text, ents = subst(s["welcome"]["text"], s["welcome"]["ents"], {
            "name": (name, {"type": "text_mention", "user": {"id": u["id"], "is_bot": False, "first_name": name}}),
            "chat": (c["title"], None)})
        old = self.last_welcome.pop(c["id"], None)
        if old:
            await self.delete(c["id"], old)
        m = await self.q("sendMessage", chat_id=c["id"], text=text, entities=ents, disable_notification=True,
                         link_preview_options={"is_disabled": True})
        if m:
            self.last_welcome[c["id"]] = m["message_id"]
            self.trash(c["id"], m["message_id"], s["welcome_del"])

    async def on_member(self, up):
        chat, old, new = up["chat"], up["old_chat_member"], up["new_chat_member"]
        u = new["user"]
        was, now_in = inside(old), inside(new)
        if chat["type"] == "channel":
            ch = self.db.one("SELECT * FROM channels WHERE id=?", chat["id"])
            if not ch or u.get("is_bot"):
                return
            if now_in and not was:
                lk = (up.get("invite_link") or {}).get("name") or ""
                miss = self.op_missing.get(u["id"])
                if lk.startswith("Метроном") or (miss and chat["id"] in miss[1] and self.now() - miss[0] < 3600):
                    self.op_joined(chat["id"], u["id"])
                    self.seen(u)
            elif was and not now_in:
                self.subcache.pop((chat["id"], u["id"]), None)
                cur = self.db.x("UPDATE op_joins SET left=? WHERE channel=? AND uid=? AND left IS NULL", self.now(),
                                chat["id"], u["id"])
                if cur.rowcount:
                    self.stat(chat["id"], "op_left")
            return
        if u["id"] == self.me_id:
            return
        if "administrator" in (old["status"], new["status"]) or "creator" in (old["status"], new["status"]):
            self.admins.pop(chat["id"], None)
        c = self.chat(chat["id"])
        if not c or not c["active"]:
            return
        by = up.get("from")
        if now_in and not was:
            await self.on_join(c, u, by if by and by["id"] != u["id"] else None)
        elif was and not now_in:
            await self.on_left(c, u)

    def migrate(self, old, new):
        if self.db.one("SELECT 1 FROM chats WHERE id=?", new):
            return
        for t, col in (("chats", "id"), ("members", "chat"), ("warns", "chat"), ("log", "chat"), ("stats", "chat"),
                       ("captcha", "chat"), ("trash", "chat")):
            self.db.x(f"UPDATE OR IGNORE {t} SET {col}=? WHERE {col}=?", new, old)
        for ch in self.db.q("SELECT id, chats FROM channels"):
            lst = json.loads(ch["chats"] or '"all"')
            if isinstance(lst, list) and old in lst:
                self.db.x("UPDATE channels SET chats=? WHERE id=?", json.dumps([new if x == old else x for x in lst]),
                          ch["id"])
        for p in self.db.q("SELECT id, s FROM posts"):
            s = json.loads(p["s"] or "{}")
            if isinstance(s.get("p_chats"), list) and old in s["p_chats"]:
                s["p_chats"] = [new if x == old else x for x in s["p_chats"]]
                self.db.x("UPDATE posts SET s=? WHERE id=?", json.dumps(s, ensure_ascii=False), p["id"])

    # ── бот в чатах и каналах ──
    async def refresh_rights(self, cid):
        m = await self.q("getChatMember", chat_id=cid, user_id=self.me_id)
        r = {}
        if m and m["status"] == "administrator":
            r = {k: bool(m.get(k)) for k, _ in RIGHTS}
            r["admin"] = True
        info = await self.q("getChat", chat_id=cid) or {}
        if info.get("linked_chat_id"):
            r["linked"] = info["linked_chat_id"]                # канал, к которому привязан чат-обсуждение
        self.db.x("UPDATE chats SET rights=? WHERE id=?", json.dumps(r), cid)
        n = await self.q("getChatMemberCount", chat_id=cid)
        if n:
            self.db.x("UPDATE chats SET members=? WHERE id=?", n, cid)
        self.admins.pop(cid, None)
        return r

    async def register_chat(self, chat, by):
        cid = chat["id"]
        tpl = [c for c in self.chats() if c["id"] != cid]
        if self.db.one("SELECT 1 FROM chats WHERE id=?", cid):
            self.db.x("UPDATE chats SET active=1, title=?, username=? WHERE id=?", chat.get("title") or "?",
                      chat.get("username"), cid)
            tpl = []
        else:
            s = tpl[0]["s"] if tpl else defaults(CHAT_KEYS)
            self.db.x("INSERT INTO chats(id, title, username, s, added) VALUES(?,?,?,?,?)", cid, chat.get("title") or "?",
                      chat.get("username"), json.dumps(s, ensure_ascii=False), self.now())
        miss = missing_rights(await self.refresh_rights(cid))
        text = f"✅ Чат <b>{esc(chat.get('title'))}</b> подключён." + \
               (f" Настройки скопированы из «{esc(tpl[0]['title'])}»." if tpl else "") + \
               ("\n\n⚠️ Боту не хватает прав админа: " + ", ".join(miss) + ". Дай их в настройках чата → Администраторы."
                if miss else "\nПрава в порядке.")
        await self.to_owners(text, kb([("⚙️ Настроить чат", f"c:{cid}")]))

    async def on_my_member(self, up):
        chat, by, new = up["chat"], up.get("from") or {}, up["new_chat_member"]
        if chat["type"] == "channel":
            return await self.on_my_channel(chat, by, new["status"])
        if chat["type"] not in ("group", "supergroup"):
            return
        c = self.chat(chat["id"])
        if not inside(new):
            if c and c["active"]:
                self.db.x("UPDATE chats SET active=0 WHERE id=?", chat["id"])
                await self.to_owners(f"🚪 Бота убрали из чата <b>{esc(c['title'])}</b>"
                                     + (f" — {self.who(by['id'])}." if by.get("id") else "."))
            return
        if c and c["active"]:
            was = missing_rights(c["rights"])
            miss = missing_rights(await self.refresh_rights(chat["id"]))
            if was != miss:
                await self.to_owners(f"🔑 Права бота в <b>{esc(c['title'])}</b>: " +
                                     ("всё в порядке ✅" if not miss else "не хватает — " + ", ".join(miss)))
            return
        if self.is_owner(by.get("id")):
            await self.register_chat(chat, by)
        elif self.cfg["leave_foreign"]:
            await self.send(chat["id"], "Этот бот работает только в чатах своего владельца. Пока! 👋")
            await self.q("leaveChat", chat_id=chat["id"])
            await self.to_owners(f"ℹ️ {self.who(by.get('id', 0))} добавил бота в чужой чат «{esc(chat.get('title'))}» — "
                                 "бот вышел. Чтобы подключить чат, добавь бота сам (после /login).")

    async def make_link(self, ch_id, name="Метроном ОП"):
        r = await self.q("createChatInviteLink", chat_id=ch_id, name=name[:32])
        if r:
            return r["invite_link"]
        ch = await self.q("getChat", chat_id=ch_id) or {}
        if ch.get("username"):
            return f"https://t.me/{ch['username']}"
        return ch.get("invite_link") or ""

    async def check_channel(self, ref, need_admin_uid=None):
        """→ (chat, ошибка). Канал/группа, где бот админ (и need_admin_uid тоже админ)."""
        try:
            ch = await self.api("getChat", chat_id=ref)
        except ApiError:
            return None, "Не вижу такой канал. Добавь бота в канал админом и пришли ещё раз."
        except Exception:
            return None, "Telegram не ответил, попробуй ещё раз через минуту."
        if ch["type"] not in ("channel", "supergroup"):
            return None, "Это не канал."
        me = await self.q("getChatMember", chat_id=ch["id"], user_id=self.me_id)
        if not me or me["status"] != "administrator":
            return None, f"Бот не админ в «{esc(ch.get('title'))}». Добавь его админом (хватит права «Пригласительные ссылки»)."
        if need_admin_uid:
            m = await self.q("getChatMember", chat_id=ch["id"], user_id=need_admin_uid)
            if not m or m["status"] not in ("creator", "administrator"):
                return None, "Ты не админ этого канала — продвигать можно только свой канал."
        return ch, ""

    async def add_own_channel(self, ref, uid):
        ch, err = await self.check_channel(ref)
        if not ch:
            return None, err
        if self.db.one("SELECT 1 FROM channels WHERE id=? AND kind='own'", ch["id"]):
            return ch, "Этот канал уже в ОП."
        link = await self.make_link(ch["id"])
        self.db.x("INSERT OR REPLACE INTO channels(id, title, username, link, kind, chats, active, until, order_id, added, "
                  "broken) VALUES(?,?,?,?,'own','\"all\"',1,NULL,NULL,?,'')", ch["id"], ch.get("title") or "?",
                  ch.get("username"), link, self.now())
        return ch, ""

    async def on_my_channel(self, chat, by, st):
        ch = self.db.one("SELECT * FROM channels WHERE id=?", chat["id"])
        uid = by.get("id")
        if st == "administrator":
            if ch:
                link = ch["link"] or await self.make_link(chat["id"])
                self.db.x("UPDATE channels SET broken='', title=?, username=?, link=? WHERE id=?", chat.get("title") or "?",
                          chat.get("username"), link, chat["id"])
                if ch["broken"]:
                    await self.to_owners(f"✅ Канал <b>{esc(chat.get('title'))}</b> снова в ОП.")
                return
            o = self.draft_op(uid)
            if o:
                return await self.attach_channel(o, chat["id"])
            if self.is_owner(uid):
                await self.send(uid, f"📢 Бот стал админом в <b>{esc(chat.get('title'))}</b>. Добавить канал в обязательную "
                                     "подписку?", kb([("➕ Добавить в ОП", f"chadd:{chat['id']}")], [("Не надо", "m")]))
            return
        if ch and ch["active"]:
            await self.channel_broken(ch, "бота убрали из админов канала")

    # ── команды в чатах ──
    def target(self, c, m, args):
        """Кому команда: ответом или @ник / ID / упоминание → (id, имя, остальные аргументы)."""
        r = m.get("reply_to_message")
        if r and "forum_topic_created" not in r:
            sc = r.get("sender_chat")
            if sc and sc["id"] != c["id"] and not r.get("is_automatic_forward"):
                return sc["id"], sc.get("title") or "канал", args
            fu = r.get("from")
            if fu and not sc:
                self.seen(fu)
                return fu["id"], uname(fu), args
        if args:
            for e in m.get("entities") or []:
                if e["type"] == "text_mention" and e.get("user"):
                    return e["user"]["id"], uname(e["user"]), args[1:]
            u = self.find_user(args[0])
            if u:
                return u["id"], u["name"], args[1:]
        return None, None, args

    async def group_cmd(self, c, m):
        parts = (m.get("text") or "").split()
        cmd, _, at = parts[0][1:].partition("@")
        if at and at.lower() != self.username.lower():
            return False
        cmd, args, cid = cmd.lower(), parts[1:], c["id"]
        u = m.get("from") or {}
        uid = u.get("id")
        anon = (m.get("sender_chat") or {}).get("id") == cid
        if cmd in ("rules", "правила"):
            await self.delete(cid, m["message_id"])
            text, ents = self.rules(c)
            r = await self.q("sendMessage", chat_id=cid, text=text, entities=ents, disable_notification=True,
                             link_preview_options={"is_disabled": True})
            if r:
                self.trash(cid, r["message_id"], 120)
            return True
        if cmd == "report":
            await self.delete(cid, m["message_id"])
            await self.report(c, m, u)
            return True
        if cmd not in MOD_CMDS:
            return False
        await self.delete(cid, m["message_id"])
        role = self.role(uid)
        if not (role or (c["s"]["admin_cmds"] and (anon or uid in await self.chat_admins(cid)))):
            return True
        if cmd in STAFF_CMDS and not role:
            await self.notice(c, uid, "cmd", "Эта команда — только для владельца и помощников.", force=True)
            return True
        try:
            res = await self.mod_cmd(c, m, cmd, args, None if anon else uid)
        except Exception:
            log.exception("cmd %s", cmd)
            res = "Что-то пошло не так, смотри лог бота."
        if res:
            await self.notice(c, uid, "cmd", res, ttl=max(60, c["s"]["notice_del"]), force=True)
        return True

    async def mod_cmd(self, c, m, cmd, args, by):
        cid, s = c["id"], c["s"]
        r = m.get("reply_to_message")
        if cmd == "help":
            return HELP_GROUP
        if cmd == "id":
            t, name, _ = self.target(c, m, args)
            return f"Чат: <code>{cid}</code>" + (f"\n{esc(name)}: <code>{t}</code>" if t else "")
        if cmd == "setlog":
            if not self.is_owner(by):
                return "Только владелец."
            if args and args[0].lower() in ("off", "выкл"):
                self.db.set("log_chat", None)
                return "Журнал выключен."
            self.db.set("log_chat", cid)
            self.db.x("UPDATE chats SET active=0 WHERE id=?", cid)
            return "📜 Теперь сюда идёт журнал действий. Модерация в этом чате выключена."
        if cmd == "cd":
            if not args:
                return f"⏱ Кулдаун здесь: {show_value(FIELDS['cooldown'], s['cooldown'])}. Поменять: /cd 10m (0 — выкл)."
            d = 0 if args[0].lower() in ("off", "выкл") else parse_dur(args[0])
            if d is None:
                return "Не понял срок. Пример: /cd 10m, /cd 1h, /cd 0"
            s["cooldown"] = d
            self.save_chat_s(cid, s)
            return f"⏱ Кулдаун: {fmt_dur(d) if d else 'выключен'}."
        if cmd == "ro":
            if args and args[0].lower() in ("off", "выкл", "0"):
                return "🔓 Чат открыт." if await self.unlock(c) else "Чат и так открыт."
            d = parse_dur(args[0]) if args else 0
            if d is None:
                return "Не понял срок. Пример: /ro 30m, /ro 2h, /ro (до /ro off)"
            if c["night"]:
                await self.unlock(c, quiet=True)
                c = self.chat(cid)
            ok = await self.lock(c, "ro", self.now() + d if d else None)
            return None if ok else "Не получилось: у бота нет права «банить» (менять разрешения)."
        if cmd == "unpin":
            ok = await self.q("unpinChatMessage", chat_id=cid, **({"message_id": r["message_id"]} if r else {}))
            return None if ok else "Не получилось открепить."
        if cmd == "del":
            if not r:
                return "Ответь /del на сообщение."
            await self.delete(cid, r["message_id"])
            return None
        if cmd == "purge":
            if not r:
                return "Ответь /purge на первое сообщение, которое надо удалить."
            ids = list(range(r["message_id"], m["message_id"]))[-500:]
            for i in range(0, len(ids), 100):
                await self.q("deleteMessages", chat_id=cid, message_ids=ids[i:i + 100])
            return f"🧹 Почистил {plural(len(ids), 'сообщение', 'сообщения', 'сообщений')}."
        if cmd == "pin":
            if not r:
                return "Ответь /pin на сообщение."
            ok = await self.q("pinChatMessage", chat_id=cid, message_id=r["message_id"],
                              disable_notification=not (args and args[0].lower() in ("loud", "громко")))
            return None if ok else "Не получилось: нужно право «закреплять»."
        t, name, rest = self.target(c, m, args)
        if not t:
            return "Кому? Ответь командой на сообщение или напиши @ник / ID после команды."
        if t == self.me_id:
            return "Себя не трогаю 🙂"
        who = esc(name) if t < 0 else mention(t, name)
        punish = cmd in ("ban", "mute", "kick", "warn", "gban")
        if punish and t > 0 and (self.role(t) or t in await self.chat_admins(cid)):
            return "Не могу: это админ или помощник."
        if cmd == "info":
            return self.user_card_short(c, t, name)
        if cmd == "warns":
            ws = self.warns(c, t)
            if not ws:
                return f"У {who} нет варнов."
            return f"⚠️ {who}: {len(ws)}/{s['warn_limit']}\n" + "\n".join(
                f"• {self.dt(w['ts'])} — {esc(w['reason'] or 'без причины')}" for w in ws)
        if cmd == "unwarn":
            ws = self.warns(c, t)
            if not ws:
                return f"У {who} нет варнов."
            self.db.x("DELETE FROM warns WHERE id=?", ws[-1]["id"])
            await self.act_log(cid, t, "↩️ Снят варн", "", by)
            return f"↩️ С {who} снят варн. Осталось {len(ws) - 1}/{s['warn_limit']}."
        if cmd in ("vip", "unvip"):
            if cmd == "unvip":
                self.db.x("DELETE FROM vip WHERE uid=?", t)
                return f"⭐ VIP у {who} снят."
            days = int(rest[0]) if rest and rest[0].isdigit() else 30
            until = self.grant_vip(t, days, f"выдал {by}")
            return f"⭐ {who} — VIP {'навсегда' if not until else 'до ' + self.dt(until)}."
        if cmd == "ungban":
            self.db.x("DELETE FROM gbans WHERE uid=?", t)
            for x in self.chats():
                await self.unban(x["id"], t, quiet=True)
            await self.act_log(0, t, "✅ Снят глобальный бан", "", by)
            return f"✅ {who} разбанен во всех чатах."
        if cmd == "gban":
            reason = " ".join(rest)
            await self.gban(t, reason, by)
            if r:
                await self.delete(cid, r["message_id"])
            return f"⛔ {who} забанен во всех чатах ({len(self.chats())})." + (f"\nПричина: {esc(reason)}" if reason else "")
        if t < 0:
            if cmd == "ban":
                ok = await self.q("banChatSenderChat", chat_id=cid, sender_chat_id=t)
                if r:
                    await self.delete(cid, r["message_id"])
                return f"⛔ Канал {who} забанен." if ok else "Не получилось."
            if cmd == "unban":
                ok = await self.q("unbanChatSenderChat", chat_id=cid, sender_chat_id=t)
                return f"✅ Канал {who} разбанен." if ok else "Не получилось."
            return "С каналом так нельзя — только /ban и /unban."
        if cmd == "ban":
            d, reason = dur_reason(rest, 0)
            ok = await self.ban(cid, t, d, reason, by)
            if ok and r:
                await self.delete(cid, r["message_id"])
            return (f"⛔ {who} — бан {'навсегда' if not d else 'на ' + fmt_dur(d)}." + (f"\nПричина: {esc(reason)}" if reason
                                                                                      else "")) if ok else \
                "Не получилось: нужно право «банить»."
        if cmd == "unban":
            ok = await self.unban(cid, t, by)
            return f"✅ {who} разбанен." if ok else "Не получилось (или он не в бане)."
        if cmd == "mute":
            d, reason = dur_reason(rest, 3600)
            ok = await self.mute(cid, t, d, reason, by)
            return (f"🔇 {who} — мут {'навсегда' if not d else 'на ' + fmt_dur(d)}." + (f"\nПричина: {esc(reason)}" if reason
                                                                                      else "")) if ok else \
                "Не получилось: нужно право «банить»."
        if cmd == "unmute":
            self.op_muted.discard((cid, t))
            ok = await self.unmute(cid, t, by)
            return f"🔊 {who} может писать." if ok else "Не получилось."
        if cmd == "kick":
            ok = await self.kick(cid, t, " ".join(rest), by)
            if ok and r:
                await self.delete(cid, r["message_id"])
            return f"👢 {who} выкинут из чата." if ok else "Не получилось: нужно право «банить»."
        if cmd == "warn":
            reason = " ".join(rest)
            if r:
                await self.delete(cid, r["message_id"])
            n, pun = await self.warn(c, t, reason, by)
            return f"⚠️ {who}: варн {n}/{s['warn_limit']}" + (f" → {pun}" if pun else "") + \
                (f"\nПричина: {esc(reason)}" if reason else "")
        return None

    def user_card_short(self, c, uid, name):
        mem = self.member(c["id"], uid)
        ws = self.warns(c, uid)
        v = self.db.one("SELECT until FROM vip WHERE uid=?", uid)
        u = self.user(uid) or {}
        return (f"👤 {mention(uid, name)} <code>{uid}</code>" + (f" · @{u['username']}" if u.get("username") else "") +
                f"\nВ чате с: {self.dt(mem['joined']) if mem['joined'] else 'давно'} · сообщений: {mem['msgs']}"
                f"\nВарны: {len(ws)}/{c['s']['warn_limit']}" +
                (f"\n⭐ VIP {'навсегда' if not v['until'] else 'до ' + self.dt(v['until'])}" if self.is_vip(uid) and v else "") +
                ("\n⛔ В глобальном бане" if self.gbanned(uid) else ""))

    def grant_vip(self, uid, days, note=""):
        """Продлить VIP на days суток (0 — навсегда) → новый until (0 = навсегда)."""
        r = self.db.one("SELECT until FROM vip WHERE uid=?", uid)
        if r and r["until"] == 0:
            return 0
        if not days:
            until = 0
        else:
            until = max(self.now(), r["until"] if r else 0) + days * 86400
        self.db.x("INSERT OR REPLACE INTO vip(uid, until, note) VALUES(?,?,?)", uid, until, note)
        return until

    async def gban(self, uid, reason="", by=None):
        self.db.x("INSERT OR REPLACE INTO gbans(uid, reason, by, ts) VALUES(?,?,?,?)", uid, reason, by, self.now())
        for x in self.chats():
            await self.ban(x["id"], uid, 0, reason, by, quiet=True)
        await self.act_log(0, uid, "⛔ Глобальный бан", reason, by)

    def rules(self, c):
        s = c["s"]
        if s["rules"]["text"]:
            return s["rules"]["text"], s["rules"]["ents"]
        lines = [f"📜 Правила чата «{c['title']}»", ""]
        chs = self.op_channels(c["id"]) if s["op"] else []
        if chs:
            lines.append("• Чтобы писать, подпишись на: " + ", ".join(
                ("@" + ch["username"]) if ch["username"] else ch["title"] for ch in chs))
        if s["cooldown"]:
            lines.append(f"• Одно {'рекламное ' if s['cd_mode'] == 'ads' else ''}сообщение раз в {fmt_dur(s['cooldown'])}")
        if s["links"] != "allow":
            lines.append({"none": "• Ссылки запрещены", "tg": "• Ссылки — только на Telegram",
                          "white": "• Ссылки — только на разрешённые сайты"}[s["links"]])
        if s["forwards"] != "all":
            lines.append("• Пересылки запрещены" if s["forwards"] == "none" else "• Без пересылок из каналов")
        if not s["as_channel"]:
            lines.append("• Писать от имени канала нельзя")
        off = [lbl for k, lbl in MEDIA if not s["media"].get(k, True)]
        if off:
            lines.append("• Нельзя: " + ", ".join(off))
        if s["maxlen"]:
            lines.append(f"• Не длиннее {s['maxlen']} символов")
        if s["stop"]:
            lines.append("• Запрещённые слова удаляются" + ("" if s["stop_action"] == "delete" else " с наказанием"))
        if s["flood_on"]:
            lines.append(f"• Больше {s['flood_n'] - 1} сообщений за {fmt_dur(s['flood_sec'])} — мут за флуд")
        lines.append(f"• {s['warn_limit']} варна → {dict(FIELDS['warn_action']['opts'])[s['warn_action']]}")
        if s["night_on"]:
            lines.append(f"• С {s['night_from']:02d}:00 до {s['night_to']:02d}:00 чат закрыт на ночь")
        if self.cfg["shop_open"]:
            lines += ["", f"📣 Реклама в чате: @{self.username}"]
        return "\n".join(lines), []

    async def report(self, c, m, u):
        cid, r = c["id"], m.get("reply_to_message")
        if not r or not r.get("from") or r["from"]["id"] == self.me_id:
            return await self.notice(c, u.get("id"), "report", "Чтобы пожаловаться, ответь /report на сообщение-нарушение.")
        if self.now() - self.notice_t.get((cid, u.get("id"), "report_ok"), 0) < 60:
            return
        self.notice_t[(cid, u.get("id"), "report_ok")] = self.now()
        t, mid = r["from"]["id"], r["message_id"]
        snippet = (r.get("text") or r.get("caption") or f"[{MEDIA_NAME.get(media_kind(r) or '', 'сообщение')}]")[:300]
        link = msg_link(cid, mid, c["username"])
        text = (f"🚨 <b>Жалоба</b> · {esc(c['title'])}\nОт: {self.who(u.get('id'))}\nНа: {mention(t, uname(r['from']))} "
                f"<code>{t}</code>\n\n«{esc(snippet)}»")
        markup = kb([("🗑 Удалить", f"rp:{cid}:{mid}:{t}:d"), ("⚠️ Варн", f"rp:{cid}:{mid}:{t}:w")],
                    [("🔇 Мут 1 ч", f"rp:{cid}:{mid}:{t}:m"), ("⛔ Бан", f"rp:{cid}:{mid}:{t}:b")],
                    [("👀 К сообщению", "url:" + link)] if link else [], [("✅ Всё ок", f"rp:{cid}:{mid}:{t}:ok")])
        if self.cfg["reports_pm"]:
            await self.to_staff(text, markup)
        lc = self.db.get("log_chat")
        if lc:
            await self.send(lc, text, markup if not self.cfg["reports_pm"] else None)
        await self.notice(c, u.get("id"), "report", "✅ Жалоба отправлена админам.", force=True)

    async def cb_report(self, cq, parts):
        cid, mid, t, a = int(parts[1]), int(parts[2]), int(parts[3]), parts[4]
        by = cq["from"]["id"]
        c = self.chat(cid)
        if not c:
            return "Чат не найден"
        done = {"ok": "✅ Всё ок"}
        if a in ("d", "w", "b"):
            await self.delete(cid, mid)
            done["d"] = "🗑 Удалено"
        if a == "w":
            n, pun = await self.warn(c, t, "жалоба", by)
            done["w"] = f"⚠️ Варн {n}/{c['s']['warn_limit']}" + (f" → {pun}" if pun else "")
        elif a == "m":
            await self.mute(cid, t, 3600, "жалоба", by)
            done["m"] = "🔇 Мут 1 ч"
        elif a == "b":
            await self.ban(cid, t, 0, "жалоба", by)
            done["b"] = "⛔ Бан"
        msg = cq["message"]
        await self.q("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"], parse_mode="HTML",
                     text=(msg.get("text") or "")[:3500] + f"\n\n{done.get(a, '✅')} — {esc(uname(cq['from']))}",
                     link_preview_options={"is_disabled": True})
        return "Готово"

    # ── закрыть / открыть чат (ночь и /ro) ──
    async def lock(self, c, why, until=None):
        ch = await self.q("getChat", chat_id=c["id"])
        perms = (ch or {}).get("permissions") or {k: True for k in ("can_send_messages", "can_send_audios",
                                                                      "can_send_documents", "can_send_photos",
                                                                      "can_send_videos", "can_send_video_notes",
                                                                      "can_send_voice_notes", "can_send_polls",
                                                                      "can_send_other_messages",
                                                                      "can_add_web_page_previews", "can_invite_users")}
        ok = await self.q("setChatPermissions", chat_id=c["id"], permissions=NO_PERMS, use_independent_chat_permissions=True)
        if not ok:
            return False
        s = c["s"]
        text = (f"🌙 Ночной режим: чат закрыт до {s['night_to']:02d}:00." if why == "night" else
                "🔒 Чат временно закрыт" + (f" до {self.local(until).strftime('%H:%M')}." if until else "."))
        m = await self.send(c["id"], text, disable_notification=True)
        self.db.x("UPDATE chats SET night=? WHERE id=?", json.dumps({"perms": perms, "why": why, "until": until,
                                                                     "mid": m and m["message_id"]}), c["id"])
        return True

    async def unlock(self, c, quiet=False):
        if not c["night"]:
            return False
        st = json.loads(c["night"])
        perms = {k: v for k, v in (st.get("perms") or {}).items() if k.startswith("can_")}
        ok = await self.q("setChatPermissions", chat_id=c["id"], permissions=perms or {"can_send_messages": True},
                          use_independent_chat_permissions=True)
        if not ok and not quiet:
            return False
        self.db.x("UPDATE chats SET night=NULL WHERE id=?", c["id"])
        if st.get("mid"):
            await self.delete(c["id"], st["mid"])
        if not quiet:
            m = await self.send(c["id"], "☀️ Доброе утро! Чат открыт." if st.get("why") == "night" else "🔓 Чат открыт.",
                                disable_notification=True)
            if m:
                self.trash(c["id"], m["message_id"], 1800)
        return True

    # ───────── личка: вход, шаги, кнопки ─────────
    async def show(self, uid, text, markup=None, cq=None):
        if cq and cq.get("message"):
            try:
                return await self.api("editMessageText", chat_id=uid, message_id=cq["message"]["message_id"],
                                      text=text[:4096], parse_mode="HTML", reply_markup=markup or {"inline_keyboard": []},
                                      link_preview_options={"is_disabled": True})
            except ApiError as e:
                if "not modified" in e.desc:
                    return None
            except Exception:
                pass
        return await self.send(uid, text, markup)

    async def on_private(self, m):
        u = m["from"]
        uid = u["id"]
        self.seen(u)
        if "successful_payment" in m:
            return await self.on_paid(m)
        if "refunded_payment" in m:
            return
        text = (m.get("text") or "").strip()
        role = self.role(uid)
        st = self.steps.get(uid)
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd, arg = cmd[1:].split("@")[0].lower(), arg.strip()
            if cmd == "login":
                return await self.cmd_login(m, arg)
            if cmd == "cancel":
                self.steps.pop(uid, None)
                return await self.send(uid, "Ок, отменил.", kb([("🏠 Меню", "m" if role else "shop")]))
            if role and cmd == "logout":
                return await self.logout(uid)
            if role and cmd in ("start", "menu", "admin", "panel") and arg != "buy":
                self.steps.pop(uid, None)
                return await self.show(uid, *await self.c_m(uid, None))
            if cmd in ("start", "shop", "buy"):
                self.steps.pop(uid, None)
                return await self.show(uid, *self.shop_home(uid))
            if cmd in ("orders", "my"):
                self.steps.pop(uid, None)
                return await self.show(uid, *self.scr_my(uid))
            if cmd == "help":
                return await self.send(uid, HELP_GROUP if role else "Здесь можно купить рекламу в наших чатах: /start")
        if st:
            return await self.on_step(m, st)
        if role:
            return await self.show(uid, *await self.c_m(uid, None))
        return await self.show(uid, *self.shop_home(uid))

    def login_wait(self, uid):
        n, per = LOGIN_TRIES
        fails = [t for t in self.login_fail.get(uid, []) if self.now() - t < per]
        self.login_fail[uid] = fails
        return fails[0] + per - self.now() if len(fails) >= n else 0

    async def cmd_login(self, m, arg):
        uid = m["from"]["id"]
        if self.is_owner(uid):
            return await self.send(uid, "Ты уже вошёл.", kb([("🏠 Меню", "m")]))
        if not self.db.get("pw"):
            return await self.send(uid, "Пароль ещё не задан. Впиши PASSWORD=… в config.env рядом с ботом и перезапусти его.")
        wait = self.login_wait(uid)
        if wait:
            return await self.send(uid, f"⛔ Слишком много неверных попыток. Попробуй через {fmt_left(wait)}.")
        if arg:
            await self.delete(uid, m["message_id"])
            return await self.try_pw(m["from"], arg)
        self.steps[uid] = {"k": "pw"}
        await self.send(uid, "🔑 Пришли пароль одним сообщением — я его сразу удалю.\n/cancel — отмена.")

    async def try_pw(self, u, pw):
        uid, now = u["id"], self.now()
        self.steps.pop(uid, None)
        if self.login_wait(uid):
            return await self.send(uid, "⛔ Слишком много неверных попыток, подожди.")
        if check_pw(pw.strip(), self.db.get("pw")):
            self.login_fail.pop(uid, None)
            others = [o for o in self.staff("owner") if o != uid]
            self.db.x("INSERT OR REPLACE INTO staff(uid, role, name, since) VALUES(?,?,?,?)", uid, "owner", uname(u), now)
            for o in others:
                await self.send(o, f"🔑 В панель вошёл {mention(uid, uname(u))} <code>{uid}</code>.\nЕсли это не ты — "
                                   "выкинь его и смени пароль.", kb([("🚫 Выкинуть", f"kick:{uid}")]))
            await self.send(uid, "✅ Вход выполнен. Панель — /menu, выход — /logout.")
            return await self.show(uid, *await self.c_m(uid, None))
        self.login_fail.setdefault(uid, []).append(now)
        n = len(self.login_fail[uid])
        left = LOGIN_TRIES[0] - n
        await self.send(uid, "❌ Неверный пароль." + (f" Осталось попыток: {left}. /login — ещё раз." if left > 0 else
                                                       " Вход заблокирован на час."))
        if n == 3:
            await self.to_owners(f"⚠️ {mention(uid, uname(u))} <code>{uid}</code> трижды ввёл неверный пароль от панели.")

    async def logout(self, uid):
        self.db.x("DELETE FROM staff WHERE uid=?", uid)
        self.steps.pop(uid, None)
        await self.send(uid, "🚪 Ты вышел из панели. Войти снова — /login")

    async def on_step(self, m, st):
        uid = m["from"]["id"]
        text = (m.get("text") or m.get("caption") or "").strip()
        k = st["k"]
        if k == "pw":
            await self.delete(uid, m["message_id"])
            return await self.try_pw(m["from"], text)
        if k in BUYER_STEPS:
            return await self.buyer_step(m, st)
        role = self.role(uid)
        if not role:
            self.steps.pop(uid, None)
            return await self.show(uid, *self.shop_home(uid))
        h = getattr(self, "s_" + k, None)
        if h:
            return await h(m, st, text)
        self.steps.pop(uid, None)

    async def ask(self, uid, step, text, back="m", extra=None):
        self.steps[uid] = step
        rows = (extra or []) + [[("✖️ Отмена", back)]]
        return await self.send(uid, text + "\n\n/cancel — отмена", kb(*rows))

    async def on_callback(self, cq):
        data, uid = cq.get("data") or "", cq["from"]["id"]
        parts = data.split(":")
        p0 = parts[0]
        ans = ""
        try:
            private = (cq.get("message") or {}).get("chat", {}).get("type") == "private"
            if p0 == "cap":
                ans = await self.cb_captcha(cq, parts)
            elif p0 == "sub":
                ans = await self.cb_op(cq, int(parts[1]))
            elif p0 == "rp":
                ans = await self.cb_report(cq, parts) if self.role(uid) else "Только для админов бота"
            elif not private:
                ans = ""
            elif p0 in BUYER_CB:
                ans = await self.cb_buyer(cq, parts)
            else:
                role = self.role(uid)
                h = getattr(self, "c_" + p0, None)
                if not role:
                    ans = "Сначала войди: /login"
                elif role == "helper" and p0 not in HELPER_CB:
                    ans = "Это может только владелец"
                elif not h:
                    ans = "Кнопка устарела"
                else:
                    self.steps.pop(uid, None)
                    res = await h(uid, cq, *parts[1:])
                    if isinstance(res, tuple):
                        await self.show(uid, res[0], res[1], cq)
                        ans = res[2] if len(res) > 2 else ""
                    else:
                        ans = res
        except Exception:
            log.exception("callback %s", data)
            ans = "Ошибка — подробности в логе бота"
        p = {"callback_query_id": cq["id"]}
        if isinstance(ans, dict):
            p.update(ans)
        elif ans:
            p["text"] = ans[:200]
        await self.q("answerCallbackQuery", **p)

    # ── главное меню ──
    def sums(self, days=1, chat=None, offset=0):
        """Статистика за days дней, заканчивая (сегодня − offset) → {ключ: сумма}."""
        end = self.local() - timedelta(days=offset)
        d0, d1 = (end - timedelta(days=days - 1)).date().isoformat(), end.date().isoformat()
        sql = "SELECT k, SUM(n) n FROM stats WHERE day>=? AND day<=?" + (" AND chat=?" if chat is not None else "") + \
              " GROUP BY k"
        return {r["k"]: r["n"] for r in self.db.q(sql, d0, d1, *([chat] if chat is not None else []))}

    @staticmethod
    def deleted(st):
        return sum(v for k, v in st.items() if k.startswith("del_"))

    async def c_m(self, uid, cq):
        chats = self.chats()
        st = self.sums()
        chs = self.op_channels()
        review = self.db.one("SELECT COUNT(*) n FROM orders WHERE status='review'")["n"]
        text = (f"🛡 <b>Метроном · Страж</b>\n\n💬 Чатов: {len(chats)} · 👥 {sum(c['members'] for c in chats)}\n"
                f"📢 В ОП: {sum(1 for c in chs if c['kind'] == 'own')} своих, {sum(1 for c in chs if c['kind'] == 'paid')} "
                f"платных\n\nСегодня: ✉️ {st.get('msgs', 0)} · 🧹 удалено {self.deleted(st)} · 👋 вошло {st.get('join', 0)}"
                f"\n💰 Заработано сегодня: {stars(st.get('income', 0))}"
                + (f"\n\n🧾 Заявок ждут ответа: <b>{review}</b>" if review else ""))
        if not chats:
            text += "\n\n👉 Начни с «💬 Чаты» → «➕ Добавить чат»."
        if self.role(uid) == "helper":
            return text, kb([("👮 Модерация", "mod"), (f"🧾 Заявки ({review})", "oq")], [("📊 Статистика", "st:1")])
        return text, kb([("💬 Чаты", "ch"), ("📢 ОП-каналы", "op")], [("🗓 Объявления", "pl"), ("👮 Модерация", "mod")],
                        [("💰 Продажи" + (f" · {review} ❗" if review else ""), "shop"), ("📊 Статистика", "st:1")],
                        [("👥 Помощники", "hp"), ("⚙️ Настройки", "set")])

    async def c_noop(self, uid, cq, *a):
        return ""

    # ── чаты ──
    def add_group_url(self):
        return (f"url:https://t.me/{self.username}?startgroup=guard&admin=delete_messages+restrict_members+pin_messages+"
                "invite_users+manage_chat")

    async def c_ch(self, uid, cq):
        chats = self.chats()
        off = self.db.q("SELECT id, title FROM chats WHERE active=0")
        text = "💬 <b>Чаты</b>\n\n" + ("Выбери чат, чтобы настроить." if chats else "Пока ни одного чата.") + \
               "\n\n➕ Чтобы подключить чат, нажми «Добавить чат» и выбери группу — бот станет админом с нужными правами. " \
               "Добавлять должен ты (вошедший владелец), иначе бот выйдет."
        if off:
            text += "\n\n<i>Отключены: " + ", ".join(esc(c["title"]) for c in off[:10]) + "</i>"
        rows = [[(("⚠️ " if missing_rights(c["rights"]) else "💬 ") + c["title"][:40], f"c:{c['id']}")] for c in chats]
        return text, kb(*rows, [("➕ Добавить чат", self.add_group_url())], [("⬅️ Меню", "m")])

    async def c_c(self, uid, cq, cid):
        c = self.chat(int(cid))
        if not c:
            return await self.c_ch(uid, cq)
        s, cid = c["s"], c["id"]
        miss = missing_rights(c["rights"])
        st = self.sums(1, cid)
        chs = self.op_channels(cid)
        night = json.loads(c["night"]) if c["night"] else None
        text = (f"💬 <b>{esc(c['title'])}</b>" + (f" · @{c['username']}" if c["username"] else "") +
                f"\n<code>{cid}</code> · 👥 {c['members']}\n" +
                ("✅ Права бота в порядке" if not miss else "⚠️ Боту не хватает прав: " + ", ".join(miss)) +
                ("" if c["active"] else "\n⏸ Чат отключён (бот вышел или это журнал)") +
                ("\n🔒 Сейчас чат закрыт (" + ("ночь" if night.get("why") == "night" else "/ro") + ")" if night else "") +
                f"\n\n🔐 ОП: {'вкл' if s['op'] else 'выкл'} · каналов: {len(chs)}"
                f"\n⏱ Кулдаун: {show_value(FIELDS['cooldown'], s['cooldown'])}"
                f"\n🤖 Капча: {'вкл' if s['captcha'] else 'выкл'} · 🌙 Ночь: "
                + (f"{s['night_from']:02d}–{s['night_to']:02d}" if s["night_on"] else "выкл") +
                f"\n\n<b>Сегодня</b>: ✉️ {st.get('msgs', 0)} · 👋 +{st.get('join', 0)} / −{st.get('left', 0)}\n"
                f"🧹 удалено {self.deleted(st)}: без подписки {st.get('del_op', 0)}, кулдаун {st.get('del_cd', 0)}, "
                f"фильтры {st.get('del_flt', 0) + st.get('del_stop', 0)}, флуд {st.get('del_flood', 0)}")
        secs = [(t, f"S:c{cid}:{k}") for k, t, _ in SECTIONS["c"]]
        rows = [secs[i:i + 2] for i in range(0, len(secs), 2)]
        return text, kb(*rows, [("📋 Эти настройки → во все чаты", f"A:{cid}")],
                        [("🔄 Проверить права", f"R:{cid}"), ("🚪 Убрать бота", f"L:{cid}")], [("⬅️ Чаты", "ch")])

    async def c_R(self, uid, cq, cid):
        await self.refresh_rights(int(cid))
        t, mk = await self.c_c(uid, cq, cid)
        return t, mk, "Обновил"

    async def c_A(self, uid, cq, cid):
        c = self.chat(int(cid))
        n = len([x for x in self.chats() if x["id"] != c["id"]])
        return (f"📋 Скопировать <b>все</b> настройки чата «{esc(c['title'])}» в остальные чаты ({n})?\n"
                "Приветствие и правила тоже скопируются.",
                kb([("✅ Да, скопировать", f"AA:{cid}")], [("⬅️ Нет", f"c:{cid}")]))

    async def c_AA(self, uid, cq, cid):
        c = self.chat(int(cid))
        n = 0
        for x in self.chats():
            if x["id"] != c["id"]:
                self.save_chat_s(x["id"], c["s"])
                n += 1
        t, mk = await self.c_c(uid, cq, cid)
        return t, mk, f"Скопировано в {n} чат(ов)"

    async def c_L(self, uid, cq, cid):
        c = self.chat(int(cid))
        return (f"🚪 Бот выйдет из «{esc(c['title'])}» и перестанет его модерировать. Точно?",
                kb([("🚪 Да, выйти", f"LL:{cid}")], [("⬅️ Нет", f"c:{cid}")]))

    async def c_LL(self, uid, cq, cid):
        cid = int(cid)
        self.db.x("UPDATE chats SET active=0 WHERE id=?", cid)
        await self.q("leaveChat", chat_id=cid)
        t, mk = await self.c_ch(uid, cq)
        return t, mk, "Вышел"

    # ── движок настроек ──
    def scope(self, sc):
        """'g' — общие, 'c<id>' — чат, 'p<id>' — объявление → (настройки, сохранить(s), название, назад)."""
        if sc == "g":
            return self.cfg, lambda s: self.save_cfg(), "общие", "shop"
        if sc[0] == "c":
            c = self.chat(int(sc[1:]))
            if c:
                return c["s"], lambda s: self.save_chat_s(c["id"], s), c["title"], f"c:{c['id']}"
        if sc[0] == "p":
            p = self.post(int(sc[1:]))
            if p:
                return p["s"], lambda s: self.save_post_s(p["id"], s), p["name"], f"p:{p['id']}"
        return None

    @staticmethod
    def section_of(sc, key):
        for k, t, keys in SECTIONS[sc[0]]:
            if key in keys:
                return k, t, keys
        return None, "", []

    async def c_S(self, uid, cq, sc, sec):
        sp = self.scope(sc)
        if not sp:
            return await self.c_m(uid, cq)
        s, _, title, back = sp
        if sc == "g" and sec == "gen":
            back = "set"
        name, keys = next(((t, ks) for k, t, ks in SECTIONS[sc[0]] if k == sec), ("", []))
        hints = [f"• <b>{FIELDS[k]['label']}</b> — {esc(FIELDS[k]['hint'])}" for k in keys if FIELDS[k]["hint"]]
        text = f"{name} · <b>{esc(title)}</b>\n\n" + ("\n".join(hints) if hints else "Нажми на пункт, чтобы изменить.")
        rows = []
        for k in keys:
            f = FIELDS[k]
            v = show_value(f, s[k])
            if f["kind"] == "bool":
                rows.append([(("✅ " if s[k] else "▫️ ") + f["label"], f"F:{sc}:{k}")])
            else:
                rows.append([(f"{f['label']}: {v[:28] + '…' if len(v) > 29 else v}", f"F:{sc}:{k}")])
        extra = []
        if sc[0] == "c" and sec == "op":
            extra = [[("📢 Каналы ОП", "op")]]
        if sc[0] == "c" and sec == "wel":
            extra = [[("👁 Показать правила", f"Y:{sc}:rules")]]
        return text, kb(*rows, *extra, [("⬅️ Назад", back)])

    async def c_F(self, uid, cq, sc, key):
        sp = self.scope(sc)
        f = FIELDS.get(key)
        if not sp or not f:
            return await self.c_m(uid, cq)
        s, save, title, back = sp
        sec = self.section_of(sc, key)[0]
        if f["kind"] == "bool":
            s[key] = not s[key]
            save(s)
            t, mk = await self.c_S(uid, cq, sc, sec)
            return t, mk, f"{f['label']}: {'вкл' if s[key] else 'выкл'}"
        v = s[key]
        text = f"<b>{f['label']}</b> · {esc(title)}\n\nСейчас: <b>{esc(show_value(f, v))}</b>"
        if f["hint"]:
            text += f"\n\n<i>{esc(f['hint'])}</i>"
        rows = []
        if f["kind"] in ("choice", "multi"):
            btns = []
            for i, (ov, lbl) in enumerate(f["opts"]):
                on = (ov == v) if f["kind"] == "choice" else bool(v.get(ov))
                btns.append((("✅ " if on else ("" if f["kind"] == "choice" else "▫️ ")) + lbl, f"V:{sc}:{key}:{i}"))
            w = 1 if max(len(b[0]) for b in btns) > 22 else (3 if max(len(b[0]) for b in btns) < 9 else 2)
            rows = [btns[i:i + w] for i in range(0, len(btns), w)]
            if f["custom"]:
                rows.append([("✏️ Своё значение", f"I:{sc}:{key}")])
        elif f["kind"] == "chats":
            rows.append([(("✅ " if v == "all" else "▫️ ") + "Все чаты", f"V:{sc}:{key}:all")])
            for c in self.chats():
                on = v == "all" or c["id"] in v
                rows.append([(("✅ " if on else "▫️ ") + c["title"][:40], f"V:{sc}:{key}:{c['id']}")])
        elif f["kind"] == "text":
            t = (v or {}).get("text") or ""
            text = f"<b>{f['label']}</b> · {esc(title)}\n\n" + (f"Сейчас:\n<blockquote>{esc(t[:3000])}</blockquote>" if t
                                                              else "Сейчас: не задан") + \
                   (f"\n\n<i>{esc(f['hint'])}</i>" if f["hint"] else "")
            rows = [[("✏️ Изменить", f"I:{sc}:{key}")] + ([("👁 Как выглядит", f"Y:{sc}:{key}")] if t else [])]
            if t:
                rows.append([("🗑 Очистить", f"X:{sc}:{key}")])
        else:
            cur = ""
            if f["kind"] in ("list", "times") and v:
                cur = ", ".join(v)
            elif f["kind"] == "btns" and v:
                cur = "\n".join(" || ".join(f"{b['text']} - {b['url']}" for b in r) for r in v)
            text = f"<b>{f['label']}</b> · {esc(title)}\n\nСейчас: " + (f"\n<code>{esc(cur[:3000])}</code>" if cur
                                                                       else "пусто") + \
                   (f"\n\n<i>{esc(f['hint'])}</i>" if f["hint"] else "")
            rows = [[("✏️ Заменить", f"I:{sc}:{key}")] + ([("➕ Добавить", f"J:{sc}:{key}")] if f["kind"] == "list" else [])]
            if v:
                rows.append([("🗑 Очистить", f"X:{sc}:{key}")])
        rows.append([("⬅️ Назад", f"S:{sc}:{sec}")])
        return text, kb(*rows)

    async def c_V(self, uid, cq, sc, key, i):
        sp = self.scope(sc)
        f = FIELDS.get(key)
        if not sp or not f:
            return await self.c_m(uid, cq)
        s, save, _, _ = sp
        if f["kind"] == "chats":
            ids = [c["id"] for c in self.chats()]
            if i == "all":
                s[key] = "all" if s[key] != "all" else []
            else:
                cur = list(ids) if s[key] == "all" else [x for x in s[key] if x in ids]
                cid = int(i)
                cur = [x for x in cur if x != cid] if cid in cur else cur + [cid]
                s[key] = "all" if set(cur) >= set(ids) and ids else cur
            save(s)
            return await self.c_F(uid, cq, sc, key)
        ov = f["opts"][int(i)][0]
        if f["kind"] == "multi":
            s[key][ov] = not s[key].get(ov)
            save(s)
            return await self.c_F(uid, cq, sc, key)
        s[key] = ov
        save(s)
        t, mk = await self.c_S(uid, cq, sc, self.section_of(sc, key)[0])
        return t, mk, f"{f['label']}: {show_value(f, ov)}"

    PROMPTS = {"dur": "Пришли длительность: 30m, 2h, 1d, 1h30m (минуты можно просто числом). 0 — выключить / навсегда.",
               "int": "Пришли число.",
               "text": "Пришли новый текст одним сообщением. Жирный, курсив, ссылки, эмодзи — сохранятся.",
               "list": "Пришли через запятую (или каждое с новой строки).",
               "times": "Пришли время через запятую, например: 10:00, 15:30, 21:00",
               "btns": "Пришли кнопки — каждая с новой строки:\nТекст кнопки - https://ссылка\nДве в ряд — через ||"}

    async def c_I(self, uid, cq, sc, key, add=False):
        f = FIELDS[key]
        kind = f["custom"] if f["kind"] == "choice" else f["kind"]
        text = f"✏️ <b>{f['label']}</b>\n\n" + self.PROMPTS.get(kind, "Пришли значение.")
        if f["hint"] and f["kind"] in ("text", "list", "btns"):
            text += f"\n\n<i>{esc(f['hint'])}</i>"
        await self.ask(uid, {"k": "field", "sc": sc, "key": key, "add": bool(add)}, text, f"F:{sc}:{key}")
        return ""

    async def c_J(self, uid, cq, sc, key):
        return await self.c_I(uid, cq, sc, key, True)

    async def c_X(self, uid, cq, sc, key):
        sp = self.scope(sc)
        s, save = sp[0], sp[1]
        f = FIELDS[key]
        s[key] = {"text": "", "ents": []} if f["kind"] == "text" else []
        save(s)
        t, mk = await self.c_F(uid, cq, sc, key)
        return t, mk, "Очищено"

    async def c_Y(self, uid, cq, sc, key):
        sp = self.scope(sc)
        if key == "rules" and sc[0] == "c":
            text, ents = self.rules(self.chat(int(sc[1:])))
        else:
            v = sp[0][key]
            text, ents = subst(v["text"], v["ents"], {"name": ("Иван", {"type": "text_mention", "user": {
                "id": uid, "is_bot": False, "first_name": "Иван"}}), "chat": (sp[2], None)})
        await self.q("sendMessage", chat_id=uid, text=text or "—", entities=ents, link_preview_options={"is_disabled": True})
        return "Отправил ниже 👇"

    async def s_field(self, m, st, text):
        uid = m["from"]["id"]
        sp = self.scope(st["sc"])
        if not sp:
            self.steps.pop(uid, None)
            return
        s, save = sp[0], sp[1]
        key, f = st["key"], FIELDS[st["key"]]
        kind = f["custom"] if f["kind"] == "choice" else f["kind"]
        v, err = None, ""
        if kind == "dur":
            v = parse_dur(text)
            err = "Не понял. Примеры: 30m, 2h, 1d, 1h30m, 0" if v is None else ""
            if v is not None and key == "p_every" and v < 300:
                err = "Слишком часто — минимум 5 минут (5m)."
        elif kind == "int":
            v = int(text) if text.isdigit() else None
            err = "Нужно целое число." if v is None else ""
            if v is not None and "price" in key and not 1 <= v <= 100000:
                err = "Цена — от 1 до 100000 звёзд."
        elif kind == "text":
            if not text:
                err = "Нужен текст."
            else:
                v = {"text": m.get("text") or m.get("caption"), "ents": m.get("entities") or m.get("caption_entities") or []}
        elif kind == "list":
            items = [x.strip().lower() for x in re.split(r"[,\n]", text) if x.strip()]
            if key == "link_white":
                items = [domain_of(x) for x in items]
            v = list(dict.fromkeys((s[key] if st["add"] else []) + items))
            err = "" if items else "Пусто."
        elif kind == "times":
            out = []
            for x in re.split(r"[,\s]+", text):
                mm = re.fullmatch(r"(\d{1,2})[:.](\d{2})", x.strip())
                if x.strip() and (not mm or int(mm.group(1)) > 23 or int(mm.group(2)) > 59):
                    out = None
                    break
                if mm:
                    out.append(f"{int(mm.group(1)):02d}:{mm.group(2)}")
            v = sorted(set(out)) if out else None
            err = "" if v else "Не понял время. Пример: 10:00, 15:30"
        elif kind == "btns":
            v = parse_buttons(text)
            err = "" if v else "Не понял. Каждая кнопка с новой строки: Текст - https://ссылка"
        if err:
            return await self.send(uid, "❌ " + err + "\nПопробуй ещё раз или /cancel")
        self.steps.pop(uid, None)
        s[key] = v
        save(s)
        await self.send(uid, f"✅ Сохранено: {f['label']} — {esc(show_value(f, v))}")
        await self.show(uid, *await self.c_F(uid, None, st["sc"], key))

    # ── ОП-каналы ──
    async def c_op(self, uid, cq):
        rows, lines = [], []
        for ch in self.db.q("SELECT * FROM channels ORDER BY kind DESC, added"):
            chats = json.loads(ch["chats"] or '"all"')
            icon = "⚠️" if ch["broken"] else ("✅" if ch["active"] else "⏸")
            kind = "свой" if ch["kind"] == "own" else f"платный до {self.dt(ch['until'])} · #{ch['order_id']}"
            lines.append(f"{icon} <b>{esc(ch['title'])}</b> — {kind}, " +
                         ("все чаты" if chats == "all" else plural(len(chats), "чат", "чата", "чатов")))
            rows.append([(f"{icon} {ch['title'][:40]}", f"oc:{ch['id']}")])
        queue = self.db.q("SELECT * FROM orders WHERE kind='op' AND status='paid' ORDER BY start")
        text = ("📢 <b>Обязательная подписка</b>\n\nЧтобы писать в чатах, человек должен быть подписан на все каналы ниже. "
                "Включается в каждом чате отдельно: Чаты → чат → 🔐 Подписка.\n\n" +
                ("\n".join(lines) if lines else "Каналов пока нет.") +
                ("\n\n⏳ <b>Очередь платных:</b>\n" + "\n".join(
                    f"#{o['id']} {esc(o['data'].get('title', '?'))} — с {self.dt(o['start'])}"
                    for o in (self.order(x["id"]) for x in queue)) if queue else ""))
        if len(self.op_channels()) > 3:
            text += "\n\n<i>Совет: больше 3 каналов в ОП заметно снижают активность в чатах.</i>"
        return text, kb(*rows, [("➕ Добавить свой канал", "opadd")], [("⬅️ Меню", "m")])

    def add_channel_url(self):
        return f"url:https://t.me/{self.username}?startchannel&admin=invite_users"

    async def c_opadd(self, uid, cq):
        await self.ask(uid, {"k": "chan"}, "➕ <b>Свой канал в ОП</b>\n\n1. Добавь бота админом в канал (кнопка ниже, хватит "
                                           "права «Пригласительные ссылки»).\n2. Перешли сюда любой пост из канала или "
                                           "пришли его @username.\n\nМожно и группу — тогда люди должны в ней состоять.",
                       "op", [[("➕ Добавить бота в канал", self.add_channel_url())]])
        return ""

    def ref_from(self, m, text):
        fo = m.get("forward_origin") or {}
        if fo.get("type") == "channel":
            return fo["chat"]["id"]
        if m.get("forward_from_chat"):
            return m["forward_from_chat"]["id"]
        mm = re.search(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,31})", text)
        if mm:
            return "@" + mm.group(1)
        if re.fullmatch(r"-100\d+", text):
            return int(text)
        return None

    async def s_chan(self, m, st, text):
        uid = m["from"]["id"]
        ref = self.ref_from(m, text)
        if not ref:
            return await self.send(uid, "Не понял. Перешли пост из канала или пришли @username канала.")
        ch, err = await self.add_own_channel(ref, uid)
        if not ch or err and "уже" not in err:
            return await self.send(uid, "❌ " + err, kb([("➕ Добавить бота в канал", self.add_channel_url())],
                                                       [("✖️ Отмена", "op")]))
        self.steps.pop(uid, None)
        await self.send(uid, f"✅ <b>{esc(ch['title'])}</b> в ОП во всех чатах." if not err else err)
        await self.show(uid, *await self.c_oc(uid, None, ch["id"]))

    async def c_chadd(self, uid, cq, chid):
        ch, err = await self.add_own_channel(int(chid), uid)
        if not ch:
            return {"text": err[:200], "show_alert": True}
        t, mk = await self.c_oc(uid, cq, ch["id"])
        return t, mk, err or "Канал добавлен в ОП"

    async def c_oc(self, uid, cq, chid):
        ch = self.db.one("SELECT * FROM channels WHERE id=?", int(chid))
        if not ch:
            return await self.c_op(uid, cq)
        chats = json.loads(ch["chats"] or '"all"')
        cid = ch["id"]
        w1, w7 = self.sums(1, cid), self.sums(7, cid)
        total = self.db.one("SELECT COUNT(*) n, SUM(left IS NOT NULL) l FROM op_joins WHERE channel=?", cid)
        text = (f"📢 <b>{esc(ch['title'])}</b>" + (f" · @{ch['username']}" if ch["username"] else "") +
                f"\nТип: {'свой' if ch['kind'] == 'own' else 'платный — заказ #' + str(ch['order_id']) + ', до ' + self.dt(ch['until'])}"
                f"\nСтатус: " + (f"⚠️ не работает — {esc(ch['broken'])}" if ch["broken"] else
                                 ("✅ работает" if ch["active"] else "⏸ выключен")) +
                f"\nКнопка ведёт на: {esc(ch['link'] or '— нет ссылки')}"
                f"\nЧаты: " + ("все" if chats == "all" else ", ".join(esc((self.chat(x) or {}).get("title", x)) for x in chats)
                               or "ни одного") +
                f"\n\n👥 Пришло через ОП: сегодня {w1.get('op_join', 0)} · 7 дней {w7.get('op_join', 0)} · всего {total['n']}"
                f"\n🚪 Отписались из них: {total['l'] or 0}")
        rows = [[("⏸ Выключить" if ch["active"] else "▶️ Включить", f"ot:{cid}"),
                 ("💬 Чаты: " + ("все" if chats == "all" else str(len(chats))), f"occ:{cid}")],
                [("🔄 Проверить и обновить ссылку", f"orl:{cid}")]]
        if ch["kind"] == "paid" and ch["order_id"]:
            rows.append([(f"🧾 Заказ #{ch['order_id']}", f"o:{ch['order_id']}")])
        else:
            rows.append([("🗑 Убрать из ОП", f"odl:{cid}")])
        rows.append([("⬅️ ОП-каналы", "op")])
        return text, kb(*rows)

    async def c_ot(self, uid, cq, chid):
        self.db.x("UPDATE channels SET active=1-active WHERE id=?", int(chid))
        return await self.c_oc(uid, cq, chid)

    async def c_occ(self, uid, cq, chid):
        ch = self.db.one("SELECT * FROM channels WHERE id=?", int(chid))
        chats = json.loads(ch["chats"] or '"all"')
        rows = [[(("✅ " if chats == "all" else "▫️ ") + "Все чаты", f"ocv:{chid}:all")]]
        for c in self.chats():
            on = chats == "all" or c["id"] in chats
            rows.append([(("✅ " if on else "▫️ ") + c["title"][:40], f"ocv:{chid}:{c['id']}")])
        return f"💬 В каких чатах требовать подписку на <b>{esc(ch['title'])}</b>?", kb(*rows, [("⬅️ Назад", f"oc:{chid}")])

    async def c_ocv(self, uid, cq, chid, i):
        ch = self.db.one("SELECT * FROM channels WHERE id=?", int(chid))
        chats = json.loads(ch["chats"] or '"all"')
        ids = [c["id"] for c in self.chats()]
        if i == "all":
            chats = "all" if chats != "all" else []
        else:
            cur = list(ids) if chats == "all" else list(chats)
            x = int(i)
            cur = [y for y in cur if y != x] if x in cur else cur + [x]
            chats = "all" if ids and set(cur) >= set(ids) else cur
        self.db.x("UPDATE channels SET chats=? WHERE id=?", json.dumps(chats), int(chid))
        return await self.c_occ(uid, cq, chid)

    async def c_orl(self, uid, cq, chid):
        ch = self.db.one("SELECT * FROM channels WHERE id=?", int(chid))
        info, err = await self.check_channel(ch["id"])
        if not info:
            return {"text": re.sub("<[^>]+>", "", err)[:200], "show_alert": True}
        link = await self.make_link(ch["id"], "Метроном ОП" if ch["kind"] == "own" else f"Метроном #{ch['order_id']}")
        self.db.x("UPDATE channels SET broken='', title=?, username=?, link=? WHERE id=?", info.get("title") or ch["title"],
                  info.get("username"), link or ch["link"], ch["id"])
        t, mk = await self.c_oc(uid, cq, chid)
        return t, mk, "✅ Всё работает, ссылка обновлена"

    async def c_odl(self, uid, cq, chid):
        ch = self.db.one("SELECT * FROM channels WHERE id=?", int(chid))
        return (f"🗑 Убрать <b>{esc(ch['title'])}</b> из обязательной подписки?",
                kb([("🗑 Да, убрать", f"odd:{chid}")], [("⬅️ Нет", f"oc:{chid}")]))

    async def c_odd(self, uid, cq, chid):
        self.db.x("DELETE FROM channels WHERE id=?", int(chid))
        t, mk = await self.c_op(uid, cq)
        return t, mk, "Убрано"

    # ── объявления ──
    def save_post_s(self, pid, s):
        p = self.post(pid)
        p["s"] = s
        nxt = self.next_run(p, after=self.now())
        self.db.x("UPDATE posts SET s=?, next=? WHERE id=?", json.dumps(s, ensure_ascii=False), nxt, pid)

    def next_run(self, p, after):
        s = p["s"]
        if not s["p_on"]:
            return 0
        if s["p_mode"] == "every":
            base = p["last"] or after
            return max(after, base + s["p_every"]) if p["last"] else after + s["p_every"]
        now = self.local(after)
        best = None
        for t in s["p_times"]:
            h, mi = map(int, t.split(":"))
            d = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if d <= now:
                d += timedelta(days=1)
            best = d if best is None or d < best else best
        return best.timestamp() if best else 0

    async def c_pl(self, uid, cq):
        ps = [self.post(r["id"]) for r in self.db.q("SELECT id FROM posts ORDER BY id")]
        lines = []
        for p in ps:
            s = p["s"]
            sch = (f"каждые {fmt_dur(s['p_every'])}" if s["p_mode"] == "every" else "в " + ", ".join(s["p_times"]))
            lines.append(f"{'▶️' if s['p_on'] else '⏸'} <b>{esc(p['name'])}</b> — {sch}" +
                         (f" · след. {self.dt(p['next'])}" if s["p_on"] and p["next"] else ""))
        text = ("🗓 <b>Объявления по расписанию</b>\n\nБот сам шлёт эти сообщения в чаты: правила, реклама, "
                "напоминания.\n\n" + ("\n".join(lines) if lines else "Пока ни одного."))
        rows = [[(("▶️ " if p["s"]["p_on"] else "⏸ ") + p["name"][:40], f"p:{p['id']}")] for p in ps]
        return text, kb(*rows, [("➕ Новое объявление", "pnew")], [("⬅️ Меню", "m")])

    async def c_pnew(self, uid, cq):
        await self.ask(uid, {"k": "post"}, "➕ <b>Новое объявление</b>\n\nПришли сообщение, как оно должно выглядеть: "
                                           "текст, фото, видео, GIF или файл с подписью — оформление сохранится. Кнопки-"
                                           "ссылки и расписание настроишь следующим шагом.", "pl")
        return ""

    async def s_post(self, m, st, text):
        uid = m["from"]["id"]
        ct = content_from(m)
        if not ct:
            return await self.send(uid, "Такое не подойдёт: нужен текст, фото, видео, GIF или файл.")
        self.steps.pop(uid, None)
        if st.get("pid"):
            self.db.x("UPDATE posts SET content=? WHERE id=?", json.dumps(ct, ensure_ascii=False), st["pid"])
            await self.send(uid, "✅ Сообщение заменено.")
            return await self.show(uid, *await self.c_p(uid, None, st["pid"]))
        name = (ct["text"].split("\n")[0][:30] or {"photo": "Фото", "video": "Видео", "animation": "GIF",
                                                    "document": "Файл"}.get(ct["type"], "Объявление"))
        s = defaults(POST_KEYS)
        cur = self.db.x("INSERT INTO posts(name, content, s, next, created) VALUES(?,?,?,?,?)", name,
                        json.dumps(ct, ensure_ascii=False), json.dumps(s), self.now() + s["p_every"], self.now())
        await self.send(uid, f"✅ Объявление создано. Первая отправка — {self.dt(self.now() + s['p_every'])} "
                             f"(каждые {fmt_dur(s['p_every'])}). Поменять — «⚙️ Расписание и опции».")
        await self.show(uid, *await self.c_p(uid, None, cur.lastrowid))

    async def c_p(self, uid, cq, pid):
        p = self.post(int(pid))
        if not p:
            return await self.c_pl(uid, cq)
        s, ct = p["s"], p["content"] or {}
        sch = f"каждые {fmt_dur(s['p_every'])}" if s["p_mode"] == "every" else "в " + ", ".join(s["p_times"])
        typ = {"text": "текст", "photo": "фото", "video": "видео", "animation": "GIF", "document": "файл"}.get(ct.get("type"), "?")
        text = (f"🗓 <b>{esc(p['name'])}</b>\n\nСтатус: {'▶️ включено' if s['p_on'] else '⏸ выключено'}\n"
                f"Тип: {typ} · кнопок: {sum(len(r) for r in s['p_btns'])}\nРасписание: {sch}\n"
                f"Чаты: {show_value(FIELDS['p_chats'], s['p_chats'])}\n"
                f"Закреп: {'да' if s['p_pin'] else 'нет'} · удалять прошлое: {'да' if s['p_del'] else 'нет'} · "
                f"без звука: {'да' if s['p_silent'] else 'нет'}\n"
                f"Пропускать, если в чате тихо: {'да' if s['p_act'] else 'нет'}\n\n"
                f"Следующая отправка: {self.dt(p['next']) if s['p_on'] and p['next'] else '—'}\n"
                f"Последняя: {self.dt(p['last']) if p['last'] else 'ещё не было'}\n\n"
                f"<i>{esc((ct.get('text') or '')[:300])}</i>")
        return text, kb([("⏸ Выключить" if s["p_on"] else "▶️ Включить", f"pt:{pid}"), ("👁 Предпросмотр", f"pv:{pid}")],
                        [("🚀 Отправить сейчас", f"pn:{pid}")], [("⚙️ Расписание и опции", f"S:p{pid}:post")],
                        [("✏️ Заменить сообщение", f"pe:{pid}"), ("🏷 Название", f"pnm:{pid}")],
                        [("🗑 Удалить", f"pd:{pid}"), ("⬅️ Объявления", "pl")])

    async def c_pt(self, uid, cq, pid):
        p = self.post(int(pid))
        p["s"]["p_on"] = not p["s"]["p_on"]
        self.save_post_s(p["id"], p["s"])
        return await self.c_p(uid, cq, pid)

    async def c_pv(self, uid, cq, pid):
        p = self.post(int(pid))
        await self.send_content(uid, p["content"], {"inline_keyboard": p["s"]["p_btns"]} if p["s"]["p_btns"] else None)
        return "Предпросмотр ниже 👇"

    async def c_pn(self, uid, cq, pid):
        n = await self.send_post(self.post(int(pid)), manual=True)
        t, mk = await self.c_p(uid, cq, pid)
        return t, mk, f"Отправлено в {n} чат(ов)"

    async def c_pe(self, uid, cq, pid):
        await self.ask(uid, {"k": "post", "pid": int(pid)}, "✏️ Пришли новое сообщение для объявления.", f"p:{pid}")
        return ""

    async def c_pnm(self, uid, cq, pid):
        await self.ask(uid, {"k": "pname", "pid": int(pid)}, "🏷 Пришли новое название (видно только тебе).", f"p:{pid}")
        return ""

    async def s_pname(self, m, st, text):
        uid = m["from"]["id"]
        if not text:
            return await self.send(uid, "Нужен текст.")
        self.steps.pop(uid, None)
        self.db.x("UPDATE posts SET name=? WHERE id=?", text[:40], st["pid"])
        await self.show(uid, *await self.c_p(uid, None, st["pid"]))

    async def c_pd(self, uid, cq, pid):
        return ("🗑 Удалить объявление?", kb([("🗑 Удалить", f"pdd:{pid}:0")], [("🗑 Удалить и стереть из чатов", f"pdd:{pid}:1")],
                                           [("⬅️ Нет", f"p:{pid}")]))

    async def c_pdd(self, uid, cq, pid, wipe):
        p = self.post(int(pid))
        if p and wipe == "1":
            for cid, x in p["sent"].items():
                await self.delete(int(cid), x["mid"])
        self.db.x("DELETE FROM posts WHERE id=?", int(pid))
        t, mk = await self.c_pl(uid, cq)
        return t, mk, "Удалено"

    async def send_content(self, chat_id, ct, markup=None, silent=False):
        p = {"chat_id": chat_id, "disable_notification": silent}
        if markup:
            p["reply_markup"] = markup
        if ct["type"] == "text":
            return await self.q("sendMessage", text=ct["text"], entities=ct["ents"], **p)
        meth = {"photo": "sendPhoto", "video": "sendVideo", "animation": "sendAnimation", "document": "sendDocument"}
        return await self.q(meth[ct["type"]], caption=ct["text"], caption_entities=ct["ents"],
                            **{ct["type"]: ct["file_id"]}, **p)

    async def send_post(self, p, manual=False):
        s, n = p["s"], 0
        targets = [c for c in self.chats() if s["p_chats"] == "all" or c["id"] in s["p_chats"]]
        for c in targets:
            prev = p["sent"].get(str(c["id"]))
            if not manual and s["p_act"] and prev and c["last_msg"] <= prev["ts"]:
                continue                                    # в чате никто не писал — не спамим
            if c["night"]:
                continue
            m = await self.send_content(c["id"], p["content"], {"inline_keyboard": s["p_btns"]} if s["p_btns"] else None,
                                        s["p_silent"])
            if not m:
                continue
            n += 1
            if s["p_del"] and prev:
                await self.delete(c["id"], prev["mid"])
            if s["p_pin"]:
                await self.q("pinChatMessage", chat_id=c["id"], message_id=m["message_id"],
                             disable_notification=s["p_silent"])
            p["sent"][str(c["id"])] = {"mid": m["message_id"], "ts": self.now()}
            self.stat(c["id"], "posts")
        p["last"] = self.now()
        self.db.x("UPDATE posts SET sent=?, last=?, next=? WHERE id=?", json.dumps(p["sent"]), p["last"],
                  self.next_run(p, self.now() + 1), p["id"])
        return n

    # ── модерация ──
    async def c_mod(self, uid, cq):
        n_gb = self.db.one("SELECT COUNT(*) n FROM gbans")["n"]
        n_vip = self.db.one("SELECT COUNT(*) n FROM vip WHERE until=0 OR until>?", self.now())["n"]
        st = self.sums(1)
        text = ("👮 <b>Модерация</b>\n\nСегодня: 🧹 удалено " + str(self.deleted(st)) +
                f" · варнов {self.db.one('SELECT COUNT(*) n FROM log WHERE action=? AND ts>?', '⚠️ Варн', self.now() - 86400)['n']}"
                f"\n⛔ В глобальном бане: {n_gb} · ⭐ VIP: {n_vip}\n\nВ чатах модерируй командами (ответом на сообщение): "
                "/ban, /mute, /warn, /kick… Здесь — действия сразу во всех чатах.")
        return text, kb([("🔎 Найти человека", "mf")], [("⛔ Глобальные баны", "gb:0"), ("⭐ VIP", "vl:0")],
                        [("📜 Журнал", "jl:0"), ("📖 Команды", "mh")], [("⬅️ Меню", "m")])

    async def c_mh(self, uid, cq):
        return HELP_GROUP, kb([("⬅️ Модерация", "mod")])

    async def c_mf(self, uid, cq):
        await self.ask(uid, {"k": "find"}, "🔎 Пришли @username, ID или перешли любое сообщение этого человека.\n"
                                           "<i>По @username найду только тех, кто писал в чатах при боте.</i>", "mod")
        return ""

    async def s_find(self, m, st, text):
        uid = m["from"]["id"]
        fo = m.get("forward_origin") or {}
        u = None
        if fo.get("type") == "user":
            self.seen(fo["sender_user"])
            u = self.user(fo["sender_user"]["id"])
        elif m.get("forward_from"):
            self.seen(m["forward_from"])
            u = self.user(m["forward_from"]["id"])
        elif fo.get("type") == "hidden_user":
            return await self.send(uid, "У него скрыт аккаунт при пересылке. Пришли ID или @username.")
        else:
            u = self.find_user(text)
        if not u:
            return await self.send(uid, "Не нашёл. Пришли числовой ID (его покажет /id в чате ответом на сообщение).")
        self.steps.pop(uid, None)
        await self.show(uid, *await self.c_u(uid, None, u["id"]))

    async def c_u(self, uid, cq, tid):
        tid = int(tid)
        u = self.user(tid) or {"id": tid, "name": str(tid), "username": None, "first": 0, "last": 0}
        v = self.db.one("SELECT until FROM vip WHERE uid=?", tid)
        g = self.db.one("SELECT * FROM gbans WHERE uid=?", tid)
        lines = [f"👤 {mention(tid, u['name'])} <code>{tid}</code>" + (f" · @{u['username']}" if u.get("username") else "")]
        if u.get("first"):
            lines.append(f"Впервые: {self.dt(u['first'])} · последний раз: {self.dt(u['last'])}")
        if self.role(tid):
            lines.append("🛡 " + ("Владелец" if self.is_owner(tid) else "Помощник"))
        if v and self.is_vip(tid):
            lines.append("⭐ VIP " + ("навсегда" if not v["until"] else "до " + self.dt(v["until"])))
        if g:
            lines.append(f"⛔ Глобальный бан с {self.dt(g['ts'])}" + (f": {esc(g['reason'])}" if g["reason"] else ""))
        for c in self.chats():
            mem = self.db.one("SELECT * FROM members WHERE chat=? AND uid=?", c["id"], tid)
            ws = len(self.warns(c, tid))
            if mem or ws:
                lines.append(f"• {esc(c['title'])}: сообщений {(mem or {}).get('msgs', 0)}, варнов {ws}")
        last = self.db.q("SELECT * FROM log WHERE uid=? ORDER BY id DESC LIMIT 5", tid)
        if last:
            lines.append("\n<b>Последнее:</b>")
            lines += [f"{self.dt(x['ts'])} {esc(x['action'])}" + (f" — {esc(x['reason'])}" if x["reason"] else "")
                      for x in last]
        return "\n".join(lines), kb(
            [("🔇 Мут везде", f"um:{tid}"), ("🔊 Размут везде", f"uu:{tid}")],
            [("✅ Снять глоб. бан", f"uub:{tid}") if g else ("⛔ Бан везде", f"ub:{tid}"), ("👢 Кик отовсюду", f"uk:{tid}")],
            [("🧽 Снять варны", f"uw:{tid}"), ("⭐ VIP", f"uv:{tid}")], [("⬅️ Модерация", "mod")])

    async def c_um(self, uid, cq, tid):
        opts = [(3600, "1 ч"), (21600, "6 ч"), (86400, "1 д"), (604800, "7 д"), (0, "навсегда")]
        return ("🔇 Мут во всех чатах — на сколько?", kb([(t, f"umd:{tid}:{d}") for d, t in opts[:3]],
                                                       [(t, f"umd:{tid}:{d}") for d, t in opts[3:]], [("⬅️", f"u:{tid}")]))

    async def c_umd(self, uid, cq, tid, d):
        tid, d = int(tid), int(d)
        if self.role(tid):
            return "Это владелец или помощник"
        n = 0
        for c in self.chats():
            n += await self.mute(c["id"], tid, d, "из панели", uid)
        t, mk = await self.c_u(uid, cq, tid)
        return t, mk, f"Мут в {n} чат(ах)"

    async def c_uu(self, uid, cq, tid):
        n = 0
        for c in self.chats():
            n += await self.unmute(c["id"], int(tid), uid)
        t, mk = await self.c_u(uid, cq, tid)
        return t, mk, f"Размучен в {n} чат(ах)"

    async def c_ub(self, uid, cq, tid):
        if self.role(int(tid)):
            return "Это владелец или помощник"
        await self.gban(int(tid), "из панели", uid)
        t, mk = await self.c_u(uid, cq, tid)
        return t, mk, "Забанен во всех чатах"

    async def c_uub(self, uid, cq, tid):
        self.db.x("DELETE FROM gbans WHERE uid=?", int(tid))
        for c in self.chats():
            await self.unban(c["id"], int(tid), quiet=True)
        await self.act_log(0, int(tid), "✅ Снят глобальный бан", "", uid)
        t, mk = await self.c_u(uid, cq, tid)
        return t, mk, "Разбанен везде"

    async def c_uk(self, uid, cq, tid):
        if self.role(int(tid)):
            return "Это владелец или помощник"
        n = 0
        for c in self.chats():
            n += await self.kick(c["id"], int(tid), "из панели", uid)
        t, mk = await self.c_u(uid, cq, tid)
        return t, mk, f"Кикнут из {n} чат(ов)"

    async def c_uw(self, uid, cq, tid):
        self.db.x("DELETE FROM warns WHERE uid=?", int(tid))
        t, mk = await self.c_u(uid, cq, tid)
        return t, mk, "Варны сняты"

    async def c_uv(self, uid, cq, tid):
        return ("⭐ VIP: пишет без кулдауна и ОП, без ограничений на ссылки и длину.",
                kb([("+1 д", f"uvd:{tid}:1"), ("+7 д", f"uvd:{tid}:7"), ("+30 д", f"uvd:{tid}:30")],
                   [("Навсегда", f"uvd:{tid}:0"), ("Снять VIP", f"uvd:{tid}:-1")], [("⬅️", f"u:{tid}")]))

    async def c_uvd(self, uid, cq, tid, d):
        tid, d = int(tid), int(d)
        if d < 0:
            self.db.x("DELETE FROM vip WHERE uid=?", tid)
        else:
            self.grant_vip(tid, d, f"выдал {uid}")
        t, mk = await self.c_u(uid, cq, tid)
        return t, mk, "Готово"

    def pager(self, prefix, page, total, back):
        nav = []
        if page > 0:
            nav.append(("◀️", f"{prefix}:{page - 1}"))
        if (page + 1) * PAGE * 2 < total:
            nav.append(("▶️", f"{prefix}:{page + 1}"))
        return [nav, [("⬅️ Назад", back)]]

    async def c_jl(self, uid, cq, page="0"):
        page = int(page)
        total = self.db.one("SELECT COUNT(*) n FROM log")["n"]
        rows = self.db.q("SELECT * FROM log ORDER BY id DESC LIMIT ? OFFSET ?", PAGE * 2, page * PAGE * 2)
        titles = {c["id"]: c["title"] for c in self.chats(active=False)}
        lines = [f"{self.dt(x['ts'])} · {esc(titles.get(x['chat'], 'все чаты'))[:20]}\n{esc(x['action'])} {self.who(x['uid'])}"
                 + (f" — {esc(x['reason'])[:60]}" if x["reason"] else "") for x in rows]
        return ("📜 <b>Журнал</b>\n\n" + ("\n\n".join(lines) if lines else "Пусто.")), kb(*self.pager("jl", page, total, "mod"))

    async def c_gb(self, uid, cq, page="0"):
        page = int(page)
        total = self.db.one("SELECT COUNT(*) n FROM gbans")["n"]
        rows = self.db.q("SELECT * FROM gbans ORDER BY ts DESC LIMIT ? OFFSET ?", PAGE, page * PAGE)
        btns = [[(f"{(self.user(x['uid']) or {}).get('name') or x['uid']}"[:30] + " — снять", f"uub:{x['uid']}")]
                for x in rows]
        return (f"⛔ <b>Глобальные баны</b>: {total}\nТакой человек забанен во всех чатах и при входе в новые.",
                kb(*btns, *self.pager("gb", page, total * 2, "mod")))

    async def c_vl(self, uid, cq, page="0"):
        page = int(page)
        q = "FROM vip WHERE until=0 OR until>?"
        total = self.db.one("SELECT COUNT(*) n " + q, self.now())["n"]
        rows = self.db.q("SELECT * " + q + " ORDER BY until LIMIT ? OFFSET ?", self.now(), PAGE, page * PAGE)
        btns = [[(f"{(self.user(x['uid']) or {}).get('name') or x['uid']}"[:24] + " · " +
                  ("∞" if not x["until"] else "до " + self.dt(x["until"])), f"u:{x['uid']}")] for x in rows]
        return (f"⭐ <b>VIP</b>: {total}\nВыдать — в карточке человека (🔎 Найти) или командой /vip в чате.",
                kb(*btns, *self.pager("vl", page, total * 2, "mod")))

    # ── помощники ──
    async def c_hp(self, uid, cq):
        rows = self.db.q("SELECT * FROM staff ORDER BY role DESC, since")
        lines = [("👑 " if r["role"] == "owner" else "🛡 ") + mention(r["uid"], r["name"]) + f" <code>{r['uid']}</code>"
                 for r in rows]
        btns = [[(f"✖️ Убрать {r['name'][:30]}", f"hr:{r['uid']}")] for r in rows if r["role"] == "helper"]
        return ("👥 <b>Команда</b>\n\n" + "\n".join(lines) + "\n\n🛡 Помощник может модерировать командами во всех чатах, "
                "получает жалобы, видит модерацию, заявки и статистику. Настройки, деньги и пароль — только у владельца.\n"
                "Помощник должен хотя бы раз написать боту /start, чтобы получать жалобы."), \
            kb(*btns, [("➕ Добавить помощника", "hadd")], [("⬅️ Меню", "m")])

    async def c_hadd(self, uid, cq):
        await self.ask(uid, {"k": "helper"}, "➕ Перешли любое сообщение будущего помощника или пришли его ID / @username.",
                       "hp")
        return ""

    async def s_helper(self, m, st, text):
        uid = m["from"]["id"]
        fo = m.get("forward_origin") or {}
        u = None
        if fo.get("type") == "user":
            self.seen(fo["sender_user"])
            u = self.user(fo["sender_user"]["id"])
        elif m.get("forward_from"):
            self.seen(m["forward_from"])
            u = self.user(m["forward_from"]["id"])
        else:
            u = self.find_user(text)
        if not u:
            return await self.send(uid, "Не нашёл. Пусть он напишет боту /start, потом пришли его @username или ID.")
        if self.is_owner(u["id"]):
            return await self.send(uid, "Он и так владелец.")
        self.steps.pop(uid, None)
        self.db.x("INSERT OR REPLACE INTO staff(uid, role, name, since) VALUES(?,?,?,?)", u["id"], "helper", u["name"],
                  self.now())
        await self.send(u["id"], "🛡 Тебя назначили помощником в Метроном · Страж. Панель — /menu, команды в чатах — /help.")
        await self.show(uid, *await self.c_hp(uid, None))

    async def c_hr(self, uid, cq, tid):
        self.db.x("DELETE FROM staff WHERE uid=? AND role='helper'", int(tid))
        await self.send(int(tid), "Ты больше не помощник в Метроном · Страж.")
        t, mk = await self.c_hp(uid, cq)
        return t, mk, "Убран"

    # ── настройки ──
    async def c_set(self, uid, cq):
        owners = self.db.q("SELECT * FROM staff WHERE role='owner'")
        lc = self.db.get("log_chat")
        lct = (self.chat(lc) or {}).get("title") if lc else None
        text = ("⚙️ <b>Настройки</b>\n\n👑 Владельцы (вошли по паролю):\n" +
                "\n".join(f"• {mention(o['uid'], o['name'])} — с {self.dt(o['since'])}" for o in owners) +
                f"\n\n📜 Журнал действий: {esc(lct or lc) if lc else 'только в панели'}\n"
                "<i>Чтобы вести журнал в отдельной группе: добавь туда бота и напиши в ней /setlog.</i>\n\n"
                f"🕐 Часовой пояс: {dict(TZS).get(self.cfg['tz'], self.cfg['tz'])} · версия {VERSION}")
        rows = [[(f"🚫 Выкинуть {o['name'][:25]}", f"kick:{o['uid']}")] for o in owners if o["uid"] != uid]
        return text, kb(*rows, [("⚙️ Общие", "S:g:gen"), ("🔑 Сменить пароль", "pw")],
                        [("💾 Бэкап сейчас", "bk"), ("♻️ Восстановить", "rs")], [("🚪 Выйти из панели", "lo")],
                        [("⬅️ Меню", "m")])

    async def c_kick(self, uid, cq, tid):
        tid = int(tid)
        if tid == uid:
            return "Это ты"
        self.db.x("DELETE FROM staff WHERE uid=? AND role='owner'", tid)
        await self.send(tid, "🚪 Тебя вышли из панели Метроном · Страж.")
        await self.send(uid, "Готово. Советую сразу сменить пароль — иначе он сможет войти снова.",
                        kb([("🔑 Сменить пароль", "pw")]))
        return "Выкинут"

    async def c_pw(self, uid, cq):
        await self.ask(uid, {"k": "newpw"}, "🔑 Пришли новый пароль (от 8 символов). Сообщение сразу удалю.\n"
                                            "Все остальные владельцы будут выкинуты из панели.", "set")
        return ""

    async def s_newpw(self, m, st, text):
        uid = m["from"]["id"]
        await self.delete(uid, m["message_id"])
        if len(text) < 8:
            return await self.send(uid, "Слишком короткий — нужно от 8 символов. Пришли ещё раз.")
        self.steps.pop(uid, None)
        self.db.set("pw", hash_pw(text))
        for o in self.staff("owner"):
            if o != uid:
                self.db.x("DELETE FROM staff WHERE uid=?", o)
                await self.send(o, "🔑 Пароль панели сменили, войди заново: /login")
        await self.send(uid, "✅ Пароль сменён. Пароль из config.env больше не действует.", kb([("🏠 Меню", "m")]))

    async def c_lo(self, uid, cq):
        await self.logout(uid)
        return "Вышел"

    async def c_bk(self, uid, cq):
        await self.backup([uid])
        return "Файл отправил ниже 👇"

    async def c_rs(self, uid, cq):
        await self.ask(uid, {"k": "restore"}, "♻️ Пришли файл бэкапа <code>guard-backup-….json</code>.\n"
                                              "⚠️ Текущие данные заменятся данными из файла.", "set")
        return ""

    async def s_restore(self, m, st, text):
        uid = m["from"]["id"]
        doc = m.get("document")
        if not doc or not doc.get("file_name", "").endswith(".json"):
            return await self.send(uid, "Нужен .json файл бэкапа.")
        try:
            data = json.loads(await self.api.download(doc["file_id"]))
            assert isinstance(data, dict) and data.get("app") == "metronom-guard" and "chats" in data["db"]
        except Exception:
            return await self.send(uid, "❌ Это не бэкап Стража (или файл повреждён).")
        self.steps.pop(uid, None)
        self.db.load(data["db"])
        self.db.x("INSERT OR REPLACE INTO staff(uid, role, name, since) VALUES(?,?,?,?)", uid, "owner",
                  uname(m["from"]), self.now())
        self.cfg = merged(GLOBAL_KEYS, self.db.get("cfg"))
        self.tz = get_tz(self.cfg["tz"])
        self.admins.clear()
        self.subcache.clear()
        await self.send(uid, f"✅ Восстановлено: чатов {len(self.chats())}, каналов ОП {len(self.op_channels())}.",
                        kb([("🏠 Меню", "m")]))

    async def backup(self, to=None):
        d = {"app": "metronom-guard", "version": VERSION, "ts": self.now(), "db": self.db.dump()}
        data = json.dumps(d, ensure_ascii=False).encode()
        name = f"guard-backup-{self.local().strftime('%Y-%m-%d_%H%M')}.json"
        for uid in to or self.staff("owner"):
            await self.q("sendDocument", chat_id=uid, caption="💾 Бэкап Метроном · Страж. Восстановить: Настройки → "
                                                              "♻️ Восстановить.", disable_notification=True,
                         _document=(name, data))

    # ── статистика ──
    async def c_st(self, uid, cq, period="1"):
        days = {"1": 1, "y": 1, "7": 7, "30": 30}.get(period, 1)
        off = 1 if period == "y" else 0
        title = {"1": "сегодня", "y": "вчера", "7": "7 дней", "30": "30 дней"}.get(period, "сегодня")
        lines = [f"📊 <b>Статистика — {title}</b>"]
        tot = self.sums(days, offset=off)
        lines.append(f"\nВсего: ✉️ {tot.get('msgs', 0)} · 👋 +{tot.get('join', 0)} / −{tot.get('left', 0)} · 🧹 "
                     f"{self.deleted(tot)}\n🤖 Капча: прошли {tot.get('captcha_ok', 0)}, отсеяно {tot.get('captcha_fail', 0)}")
        for c in self.chats():
            st = self.sums(days, c["id"], off)
            lines.append(f"\n💬 <b>{esc(c['title'])}</b> · 👥 {c['members']}\n✉️ {st.get('msgs', 0)} · 👋 +{st.get('join', 0)}"
                         f" / −{st.get('left', 0)} · 🧹 ОП {st.get('del_op', 0)}, кд {st.get('del_cd', 0)}, фильтры "
                         f"{st.get('del_flt', 0) + st.get('del_stop', 0) + st.get('del_flood', 0)}")
        chs = self.db.q("SELECT * FROM channels")
        if chs:
            lines.append("\n📢 <b>Пришло через ОП</b>")
            for ch in chs:
                st = self.sums(days, ch["id"], off)
                lines.append(f"• {esc(ch['title'])}: +{st.get('op_join', 0)}" +
                             (f", отписались {st['op_left']}" if st.get("op_left") else ""))
        if self.is_owner(uid):
            lines.append(f"\n💰 Заработано: {stars(tot.get('income', 0))} · заказов {tot.get('orders', 0)}")
        return "\n".join(lines)[:4096], kb([("Сегодня", "st:1"), ("Вчера", "st:y"), ("7 дн", "st:7"), ("30 дн", "st:30")],
                                           [("⬅️ Меню", "m")])

    # ───────── магазин: покупатель ─────────
    def earliest(self, kind, dur, exclude=0):
        """Самый ранний старт, когда все dur секунд есть свободное место (op_slots / pin_slots)."""
        slots = int(self.cfg[kind + "_slots"])
        now = self.now()
        iv = [(o["start"], o["end"]) for o in self.db.q(
            "SELECT start, end FROM orders WHERE kind=? AND status IN ('paid','live') AND end>? AND id!=?", kind, now,
            exclude)]
        for t in sorted({now} | {e for s, e in iv}):
            pts = [t] + [s for s, e in iv if t < s < t + dur]
            if all(sum(1 for s, e in iv if s <= p < e) < slots for p in pts):
                return t
        return max([now] + [e for s, e in iv])

    def avail(self, kind, days=1):
        t = self.earliest(kind, days * 86400)
        return "свободно — старт сразу после оплаты" if t <= self.now() + 60 else f"ближайшее место — с {self.dt(t)}"

    def days_opts(self):
        ds = [int(k) for k, v in self.cfg["days"].items() if v]
        return sorted(ds) or [1]

    def shop_home(self, uid):
        cfg = self.cfg
        chats = self.chats()
        if not cfg["shop_open"] or not chats:
            return ("🛡 Это бот-модератор наших чатов.\n\nПродажа рекламы сейчас закрыта — загляни позже.",
                    kb([("🧾 Мои заказы", "my")]))
        lines = ["📣 <b>Реклама в наших чатах</b>", ""]
        for c in chats:
            t = f'<a href="https://t.me/{c["username"]}">{esc(c["title"])}</a>' if c["username"] else esc(c["title"])
            lines.append(f"💬 {t} — {plural(c['members'], 'участник', 'участника', 'участников')}")
        per_day = self.sums(7).get("msgs", 0) // 7
        lines.append(f"\nОхват: <b>{sum(c['members'] for c in chats)}</b> участников · ~{per_day} сообщений в сутки")
        rows = []
        if cfg["op_sale"]:
            lines.append(f"\n📢 <b>Канал в обязательную подписку</b> — {stars(cfg['op_price'])}/сутки\nЧтобы писать в чатах, "
                         f"каждый подписывается на твой канал. Покажу, сколько пришло.\n<i>{self.avail('op')}</i>")
            rows.append([(f"📢 ОП — {stars(cfg['op_price'])}/сут", "buy:op")])
        if cfg["pin_sale"]:
            lines.append(f"\n📌 <b>Закреп</b> — {stars(cfg['pin_price'])}/сутки\nТвой пост закрепляется во всех чатах."
                         f"\n<i>{self.avail('pin')}</i>")
            rows.append([(f"📌 Закреп — {stars(cfg['pin_price'])}/сут", "buy:pin")])
        if cfg["vip_sale"]:
            lines.append(f"\n⭐ <b>VIP</b> — {stars(cfg['vip_price'])}/сутки\nПиши без кулдауна и подписки, ссылки и "
                         "длина без ограничений.")
            rows.append([(f"⭐ VIP — {stars(cfg['vip_price'])}/сут", "buy:vip")])
        if not rows:
            lines.append("\nСейчас ничего не продаётся — загляни позже.")
        lines.append("\nОплата — звёздами Telegram ⭐")
        return "\n".join(lines), kb(*rows, [("🧾 Мои заказы", "my")])

    PRODUCT_INFO = {
        "op": "Твой канал добавляется в обязательную подписку всех чатов: чтобы написать, человек подписывается. "
              "Нужно будет добавить бота админом в канал (только право «Пригласительные ссылки») — так бот проверяет "
              "подписку и считает, сколько пришло.",
        "pin": "Бот публикует твой пост во всех чатах и закрепляет его на весь срок. Потом открепит и удалит сам.",
        "vip": "Весь срок ты пишешь во всех чатах без кулдауна и обязательной подписки, ссылки и длина — без ограничений. "
               "Остальные правила (стоп-слова, флуд) действуют."}

    def scr_buy(self, kind):
        price = self.cfg[kind + "_price"]
        text = f"{'📢📌⭐'['op pin vip'.split().index(kind)]} <b>{KIND_NAME[kind]}</b> — {stars(price)}/сутки\n\n" + \
               self.PRODUCT_INFO[kind]
        if kind != "vip":
            text += f"\n\n<i>Сейчас {self.avail(kind)}.</i>"
        text += "\n\nВыбери срок:"
        btns = [(f"{dn(d)} — {stars(price * d)}", f"bd:{kind}:{d}") for d in self.days_opts()]
        return text, kb(*[btns[i:i + 2] for i in range(0, len(btns), 2)], [("⬅️ Назад", "shop")])

    def order_text(self, o, admin=False):
        d, now = o["data"], self.now()
        lines = [f"🧾 <b>Заказ #{o['id']}</b> — {KIND_NAME[o['kind']]}"]
        if admin:
            lines.append(f"Покупатель: {mention(o['uid'], o['uname'])} <code>{o['uid']}</code>")
        lines.append(f"Срок: {dn(o['days'])} · {stars(o['price'])}\nСтатус: <b>{STATUS_NAME[o['status']]}</b>")
        if o["kind"] == "op":
            lines.append("Канал: " + (esc(d.get("title")) + (f" · @{d['username']}" if d.get("username") else "")
                                      if d.get("channel") else "ещё не указан"))
        if o["kind"] == "pin":
            ct = d.get("content")
            lines.append("Пост: " + (f"«{esc((ct.get('text') or '[медиа]')[:200])}»" if ct else "ещё не прислан") +
                         (f" + {plural(sum(len(r) for r in d.get('btns') or []), 'кнопка', 'кнопки', 'кнопок')}"
                          if d.get("btns") else ""))
        if o["start"]:
            lines.append(f"Период: {self.dt(o['start'])} — {self.dt(o['end'])}")
        elif o["status"] in ("draft", "review", "approved") and o["kind"] != "vip":
            t = self.earliest(o["kind"], o["days"] * 86400)
            lines.append("Старт: " + ("сразу после оплаты" if t <= now + 60 else f"не раньше {self.dt(t)} (места заняты)"))
        if o["kind"] == "op" and o["status"] in ("live", "done") and d.get("channel"):
            j = self.db.one("SELECT COUNT(*) n, SUM(left IS NOT NULL) l FROM op_joins WHERE channel=? AND ts>=? AND ts<=?",
                            d["channel"], o["start"] or 0, o["end"] or now)
            lines.append(f"👥 Пришло через ОП: {j['n']}" + (f", отписались {j['l']}" if j["l"] else ""))
        if o["note"]:
            lines.append(f"Комментарий: {esc(o['note'])}")
        return "\n".join(lines)

    def scr_order_buyer(self, o):
        rows = []
        if o["status"] == "draft":
            ready = (o["kind"] != "op" or o["data"].get("channel")) and (o["kind"] != "pin" or o["data"].get("content"))
            if ready:
                rows.append([("📨 Отправить на проверку", f"osub:{o['id']}") if self.cfg["approve"] else
                             (f"💳 Оплатить {stars(o['price'])}", f"osub:{o['id']}")])
            elif o["kind"] == "op":
                rows.append([("➕ Добавить бота в канал", self.add_channel_url())])
        if o["status"] == "approved":
            rows.append([(f"💳 Оплатить {stars(o['price'])}", f"osub:{o['id']}")])
        if o["status"] in ("draft", "review", "approved"):
            rows.append([("✖️ Отменить заказ", f"ocx:{o['id']}")])
        rows.append([("🧾 Мои заказы", "my"), ("🏪 Магазин", "shop")])
        hint = {"draft": "\n\nПроверь и отправь 👇", "review": "\n\n⏳ Ждёт проверки — ответ придёт сюда.",
                "approved": "\n\n✅ Одобрено, осталось оплатить.", "paid": "\n\n✅ Оплачено, стоит в очереди."}
        return self.order_text(o) + hint.get(o["status"], ""), kb(*rows)

    def scr_my(self, uid):
        os_ = self.db.q("SELECT id FROM orders WHERE uid=? AND status!='draft' ORDER BY id DESC LIMIT 10", uid)
        if not os_:
            return "🧾 Заказов пока нет.", kb([("🏪 Магазин", "shop")])
        rows = []
        for r in os_:
            o = self.order(r["id"])
            rows.append([(f"#{o['id']} {KIND_NAME[o['kind']]} · {dn(o['days'])} · {STATUS_NAME[o['status']]}", f"mo:{o['id']}")])
        return "🧾 <b>Мои заказы</b>", kb(*rows, [("🏪 Магазин", "shop")])

    def draft_op(self, uid):
        r = self.db.one("SELECT id FROM orders WHERE uid=? AND kind='op' AND status='draft' ORDER BY id DESC", uid)
        o = self.order(r["id"]) if r else None
        return o if o and not o["data"].get("channel") else None

    async def cb_buyer(self, cq, parts):
        uid, p0 = cq["from"]["id"], parts[0]
        u = cq["from"]
        self.seen(u)
        if p0 == "shop":
            self.steps.pop(uid, None)
            return await self.show(uid, *self.shop_home(uid), cq) and ""
        if p0 == "my":
            return await self.show(uid, *self.scr_my(uid), cq) and ""
        if p0 == "buy":
            kind = parts[1]
            if not self.cfg["shop_open"] or not self.cfg.get(kind + "_sale"):
                return "Сейчас это не продаётся"
            return await self.show(uid, *self.scr_buy(kind), cq) and ""
        o = None
        if p0 in ("mo", "osub", "ocx", "nobtn"):
            o = self.order(int(parts[1]))
            if not o or o["uid"] != uid:
                return "Заказ не найден"
        if p0 == "mo":
            return await self.show(uid, *self.scr_order_buyer(o), cq) and ""
        if p0 == "bd":
            kind, days = parts[1], int(parts[2])
            if not self.cfg["shop_open"] or not self.cfg.get(kind + "_sale") or days not in self.days_opts():
                return "Сейчас это не продаётся"
            self.db.x("DELETE FROM orders WHERE uid=? AND status='draft'", uid)
            cur = self.db.x("INSERT INTO orders(kind, uid, uname, data, days, price, status, created) VALUES(?,?,?,?,?,?,?,?)",
                            kind, uid, uname(u), "{}", days, self.cfg[kind + "_price"] * days, "draft", self.now())
            o = self.order(cur.lastrowid)
            if kind == "op":
                self.steps[uid] = {"k": "ord_ch", "oid": o["id"]}
                await self.show(uid, self.order_text(o) + "\n\n<b>Шаг 1.</b> Добавь бота админом в свой канал — кнопка "
                                "ниже (хватит права «Пригласительные ссылки»).\n<b>Шаг 2.</b> Перешли сюда любой пост из "
                                "канала или пришли его @username.",
                                kb([("➕ Добавить бота в канал", self.add_channel_url())], [("✖️ Отменить", f"ocx:{o['id']}")]),
                                cq)
            elif kind == "pin":
                self.steps[uid] = {"k": "ord_post", "oid": o["id"]}
                await self.show(uid, self.order_text(o) + "\n\nПришли пост для закрепа одним сообщением: текст, фото, видео "
                                "или GIF с подписью. Оформление сохранится.", kb([("✖️ Отменить", f"ocx:{o['id']}")]), cq)
            else:
                await self.show(uid, *self.scr_order_buyer(o), cq)
            return ""
        if p0 == "nobtn":
            self.steps.pop(uid, None)
            return await self.show(uid, *self.scr_order_buyer(o), cq) and ""
        if p0 == "ocx":
            if o["status"] not in ("draft", "review", "approved"):
                return "Этот заказ уже не отменить — напиши владельцу"
            was = o["status"]
            self.save_order(o, status="cancelled")
            self.steps.pop(uid, None)
            if was != "draft":
                await self.to_staff(f"✖️ Покупатель отменил заказ #{o['id']}.")
            await self.show(uid, *self.scr_order_buyer(o), cq)
            return "Отменено"
        if p0 == "osub":
            return await self.submit(o, cq)
        return ""

    async def submit(self, o, cq=None):
        uid = o["uid"]
        if o["status"] == "approved":
            await self.invoice(o)
            return "Счёт ниже 👇"
        if o["status"] != "draft":
            return "Заказ уже отправлен"
        if o["kind"] == "op" and not o["data"].get("channel") or o["kind"] == "pin" and not o["data"].get("content"):
            return "Сначала заверши заказ"
        if self.cfg["approve"]:
            self.save_order(o, status="review")
            await self.show(uid, *self.scr_order_buyer(o), cq)
            mk = kb([("✅ Одобрить", f"oa:{o['id']}"), ("❌ Отклонить", f"orj:{o['id']}")],
                    [("👁 Пост", f"opv:{o['id']}")] if o["kind"] == "pin" else [], [("🧾 Открыть", f"o:{o['id']}")])
            await self.to_staff("🆕 <b>Новая заявка</b>\n\n" + self.order_text(o, admin=True), mk)
            return "Отправлено на проверку"
        self.save_order(o, status="approved")
        await self.show(uid, *self.scr_order_buyer(o), cq)
        await self.invoice(o)
        return ""

    async def invoice(self, o):
        title = f"{KIND_NAME[o['kind']]} · {dn(o['days'])}"
        desc = {"op": f"Канал «{o['data'].get('title', '')}» в обязательной подписке наших чатов на {dn(o['days'])}.",
                "pin": f"Закреп твоего поста во всех наших чатах на {dn(o['days'])}.",
                "vip": f"VIP во всех наших чатах на {dn(o['days'])}: без кулдауна и обязательной подписки."}[o["kind"]]
        await self.q("sendInvoice", chat_id=o["uid"], title=title[:32], description=desc[:255], payload=f"o:{o['id']}",
                     currency="XTR", prices=[{"label": title[:32], "amount": int(o["price"])}])

    async def buyer_step(self, m, st):
        uid = m["from"]["id"]
        o = self.order(st.get("oid") or 0)
        if not o or o["uid"] != uid or o["status"] != "draft":
            self.steps.pop(uid, None)
            return await self.show(uid, *self.shop_home(uid))
        text = (m.get("text") or m.get("caption") or "").strip()
        if st["k"] == "ord_ch":
            ref = self.ref_from(m, text)
            if not ref:
                return await self.send(uid, "Перешли пост из своего канала или пришли его @username.")
            return await self.attach_channel(o, ref)
        if st["k"] == "ord_post":
            ct = content_from(m)
            if not ct:
                return await self.send(uid, "Нужен текст, фото, видео или GIF с подписью.")
            o["data"]["content"] = ct
            self.save_order(o)
            self.steps[uid] = {"k": "ord_btn", "oid": o["id"]}
            return await self.send(uid, "Добавить под постом кнопки-ссылки? Пришли их — каждая с новой строки:\n"
                                        "<code>Текст кнопки - https://ссылка</code>\nили нажми «Без кнопок».",
                                   kb([("Без кнопок ➡️", f"nobtn:{o['id']}")]))
        if st["k"] == "ord_btn":
            btns = parse_buttons(text)
            if not btns:
                return await self.send(uid, "Не понял. Формат: <code>Текст - https://ссылка</code>, каждая с новой строки.",
                                       kb([("Без кнопок ➡️", f"nobtn:{o['id']}")]))
            o["data"]["btns"] = btns
            self.save_order(o)
            self.steps.pop(uid, None)
            await self.send_content(uid, o["data"]["content"], {"inline_keyboard": btns})
            return await self.show(uid, *self.scr_order_buyer(o))

    async def attach_channel(self, o, ref):
        uid = o["uid"]
        ch, err = await self.check_channel(ref, need_admin_uid=uid)
        if not ch:
            return await self.send(uid, "❌ " + err, kb([("➕ Добавить бота в канал", self.add_channel_url())],
                                                       [("✖️ Отменить заказ", f"ocx:{o['id']}")]))
        own = self.db.one("SELECT 1 FROM channels WHERE id=? AND kind='own'", ch["id"])
        if own:
            return await self.send(uid, "Этот канал и так в нашей обязательной подписке 🙂")
        o["data"].update(channel=ch["id"], title=ch.get("title") or "?", username=ch.get("username"))
        self.save_order(o)
        self.steps.pop(uid, None)
        await self.send(uid, f"✅ Канал «{esc(ch.get('title'))}» подключён.")
        await self.show(uid, *self.scr_order_buyer(o))

    async def on_precheckout(self, pq):
        pl = pq.get("invoice_payload") or ""
        o = self.order(int(pl[2:])) if pl.startswith("o:") and pl[2:].isdigit() else None
        ok = bool(o and o["status"] == "approved" and o["uid"] == pq["from"]["id"] and pq.get("currency") == "XTR"
                  and int(pq.get("total_amount", 0)) == int(o["price"]))
        await self.q("answerPreCheckoutQuery", pre_checkout_query_id=pq["id"], ok=ok,
                     **({} if ok else {"error_message": "Счёт устарел или уже оплачен. Открой бота и оформи заказ заново."}))

    async def on_paid(self, m):
        sp, uid, now = m["successful_payment"], m["from"]["id"], self.now()
        pl, charge = sp.get("invoice_payload") or "", sp.get("telegram_payment_charge_id")
        o = self.order(int(pl[2:])) if pl.startswith("o:") and pl[2:].isdigit() else None
        if not o or o["status"] != "approved":
            await self.q("refundStarPayment", user_id=uid, telegram_payment_charge_id=charge)
            await self.send(uid, "Этот счёт уже неактуален — звёзды вернул. Оформи заказ заново: /start")
            return await self.to_owners(f"↩️ Автовозврат {stars(sp.get('total_amount', 0))}: оплата устаревшего счёта от "
                                        f"{self.who(uid)}.")
        self.stat(0, "income", o["price"])
        self.stat(0, "orders")
        dur = o["days"] * 86400
        if o["kind"] == "vip":
            until = self.grant_vip(uid, o["days"], f"заказ #{o['id']}")
            self.save_order(o, status="live", start=now, end=until or now + dur, charge=charge)
            await self.send(uid, f"⭐ Оплачено! VIP действует до {self.dt(o['end'])}. Пиши в чатах без ограничений.")
        else:
            start = self.earliest(o["kind"], dur, exclude=o["id"])
            self.save_order(o, status="paid", start=start, end=start + dur, charge=charge)
            if start <= now + TICK:
                await self.send(uid, "✅ Оплачено! Запускаю…")
                await self.go_live(o)
            else:
                await self.send(uid, f"✅ Оплачено! Старт — {self.dt(start)}: раньше все места заняты. Напомню, когда запущу.")
        await self.to_owners(f"💰 +{stars(o['price'])} — заказ #{o['id']}: {KIND_NAME[o['kind']]} на {dn(o['days'])} от "
                             f"{mention(uid, o['uname'])}", kb([("🧾 Открыть", f"o:{o['id']}")]))

    async def go_live(self, o):
        now, d = self.now(), o["data"]
        if now - (o["start"] or now) > 120:                 # запуск задержался — срок отсчитываем заново
            o["start"], o["end"] = now, now + o["days"] * 86400
        if o["kind"] == "op":
            info, err = await self.check_channel(d["channel"])
            if not info:
                if d.get("retry", 0) <= now:
                    d["retry"] = now + 900
                    self.save_order(o)
                    msg = f"⚠️ Заказ #{o['id']} не запускается: {re.sub('<[^>]+>', '', err)}"
                    await self.send(o["uid"], msg + "\nВерни бота админом в канал — запущу сам.")
                    await self.to_owners(msg, kb([("🧾 Открыть", f"o:{o['id']}")]))
                return False
            link = await self.make_link(d["channel"], f"Метроном #{o['id']}")
            ex = self.db.one("SELECT * FROM channels WHERE id=?", d["channel"])
            if ex and ex["kind"] == "paid":
                self.db.x("UPDATE channels SET until=?, order_id=?, link=?, active=1, broken='', title=? WHERE id=?",
                          max(ex["until"] or 0, o["end"]), o["id"], link or ex["link"], info.get("title") or ex["title"],
                          ex["id"])
            elif not ex:
                self.db.x("INSERT INTO channels(id, title, username, link, kind, chats, active, until, order_id, added, "
                          "broken) VALUES(?,?,?,?,'paid','\"all\"',1,?,?,?,'')", d["channel"], info.get("title") or "?",
                          info.get("username"), link, o["end"], o["id"], now)
            d["link"] = link
            info_txt = "Канал в обязательной подписке всех чатов."
        elif o["kind"] == "pin":
            mids, loud = {}, self.cfg["pin_loud"]
            for c in self.chats():
                m = await self.send_content(c["id"], d["content"], {"inline_keyboard": d["btns"]} if d.get("btns") else None,
                                            silent=not loud)
                if m:
                    mids[str(c["id"])] = m["message_id"]
                    await self.q("pinChatMessage", chat_id=c["id"], message_id=m["message_id"], disable_notification=not loud)
            d["mids"] = mids
            info_txt = f"Пост закреплён в {plural(len(mids), 'чате', 'чатах', 'чатах')}."
        else:
            info_txt = ""
        self.save_order(o, status="live")
        await self.send(o["uid"], f"🚀 Заказ #{o['id']} запущен до {self.dt(o['end'])}. {info_txt}")
        return True

    async def finish(self, o, status="done"):
        d, rep = o["data"], ""
        if o["kind"] == "op" and d.get("channel"):
            ch = self.db.one("SELECT * FROM channels WHERE id=?", d["channel"])
            if ch and ch["kind"] == "paid" and ch["order_id"] == o["id"]:
                self.db.x("DELETE FROM channels WHERE id=?", ch["id"])
            j = self.db.one("SELECT COUNT(*) n, SUM(left IS NOT NULL) l FROM op_joins WHERE channel=? AND ts>=? AND ts<=?",
                            d["channel"], o["start"] or 0, self.now())
            d["joined"], d["left"] = j["n"], j["l"] or 0
            rep = f"\n👥 Через ОП пришло: <b>{j['n']}</b>" + (f", из них уже отписались {j['l']}" if j["l"] else "") + "."
        elif o["kind"] == "pin":
            for cid, mid in (d.get("mids") or {}).items():
                await self.q("unpinChatMessage", chat_id=int(cid), message_id=mid)
                await self.delete(int(cid), mid)
            rep = f"\nПост висел в закрепе {plural(len(d.get('mids') or {}), 'чата', 'чатов', 'чатов')}."
        self.save_order(o, status=status, end=min(o["end"] or self.now(), self.now()) if status != "done" else o["end"])
        if status == "done":
            await self.send(o["uid"], f"🏁 Заказ #{o['id']} ({KIND_NAME[o['kind']]}) завершён.{rep}\nСпасибо! Повторить — /start")

    async def refund(self, o, why=""):
        if not o["charge"]:
            return "Нет платежа — нечего возвращать"
        try:
            await self.api("refundStarPayment", user_id=o["uid"], telegram_payment_charge_id=o["charge"])
        except ApiError as e:
            return f"Telegram не дал вернуть: {e.desc}"
        if o["kind"] == "vip":
            v = self.db.one("SELECT until FROM vip WHERE uid=?", o["uid"])
            if v and v["until"]:
                left = v["until"] - o["days"] * 86400
                if left <= self.now():
                    self.db.x("DELETE FROM vip WHERE uid=?", o["uid"])
                else:
                    self.db.x("UPDATE vip SET until=? WHERE uid=?", left, o["uid"])
        if o["status"] == "live":
            await self.finish(o, "refunded")
        else:
            self.save_order(o, status="refunded")
        self.stat(0, "income", -o["price"])
        await self.send(o["uid"], f"↩️ По заказу #{o['id']} возвращено {stars(o['price'])}." + (f"\n{esc(why)}" if why else ""))
        return ""

    # ───────── магазин: владелец ─────────
    async def c_shop(self, uid, cq):
        cfg = self.cfg
        cnt = {r["status"]: r["n"] for r in self.db.q("SELECT status, COUNT(*) n FROM orders GROUP BY status")}
        live = {k: self.db.one("SELECT COUNT(*) n FROM orders WHERE kind=? AND status='live'", k)["n"] for k in ("op", "pin")}
        inc = lambda d: self.sums(d, 0).get("income", 0)
        total = self.db.one("SELECT SUM(n) n FROM stats WHERE chat=0 AND k='income'")["n"] or 0
        text = (f"💰 <b>Продажи</b>\n\nМагазин: {'✅ открыт' if cfg['shop_open'] else '⏸ закрыт'} · "
                f"{'ручная проверка' if cfg['approve'] else 'без проверки'}\n"
                f"📢 ОП: {stars(cfg['op_price'])}/сут · занято {live['op']}/{cfg['op_slots']}" + ("" if cfg["op_sale"] else " (выкл)") +
                f"\n📌 Закреп: {stars(cfg['pin_price'])}/сут · занято {live['pin']}/{cfg['pin_slots']}" +
                ("" if cfg["pin_sale"] else " (выкл)") +
                f"\n⭐ VIP: {stars(cfg['vip_price'])}/сут" + ("" if cfg["vip_sale"] else " (выкл)") +
                f"\n\n🧾 На проверке: {cnt.get('review', 0)} · ждут оплаты: {cnt.get('approved', 0)} · в очереди: "
                f"{cnt.get('paid', 0)} · в эфире: {cnt.get('live', 0)}\n"
                f"💵 Заработано: сегодня {stars(inc(1))} · 7 дней {stars(inc(7))} · 30 дней {stars(inc(30))} · всего "
                f"{stars(total)}\n\n<i>Звёзды копятся на балансе бота. Баланс и вывод (через Fragment) — в "
                "профиле бота в Telegram.</i>\n\nСсылка для покупателей:\n"
                f"<code>https://t.me/{self.username}?start=buy</code>")
        return text, kb([(f"🧾 Заявки ({cnt.get('review', 0)})", "oq"), ("📋 Все заказы", "ol:0")],
                        [("💰 Магазин", "S:g:shop"), ("📢 Места в ОП", "S:g:sop")],
                        [("📌 Закреп", "S:g:spin"), ("⭐ VIP", "S:g:svip")], [("⬅️ Меню", "m")])

    async def c_oq(self, uid, cq):
        rows = []
        for st, icon in (("review", "🆕"), ("approved", "💳"), ("paid", "⏳"), ("live", "🚀")):
            for r in self.db.q("SELECT id FROM orders WHERE status=? ORDER BY id", st):
                o = self.order(r["id"])
                extra = f" · до {self.dt(o['end'])}" if st == "live" else (f" · с {self.dt(o['start'])}" if st == "paid" else "")
                rows.append([(f"{icon} #{o['id']} {KIND_NAME[o['kind']]} · {dn(o['days'])} · {o['uname'][:15]}{extra}",
                              f"o:{o['id']}")])
        text = ("🧾 <b>Заявки и активные заказы</b>\n🆕 на проверке · 💳 ждёт оплаты · ⏳ оплачен, в очереди · 🚀 в эфире" +
                ("" if rows else "\n\nПусто."))
        return text, kb(*rows[:40], [("📋 Все заказы", "ol:0"), ("⬅️ Продажи", "shop")])

    async def c_ol(self, uid, cq, page="0"):
        page = int(page)
        total = self.db.one("SELECT COUNT(*) n FROM orders WHERE status!='draft'")["n"]
        rows = []
        for r in self.db.q("SELECT id FROM orders WHERE status!='draft' ORDER BY id DESC LIMIT ? OFFSET ?", PAGE,
                           page * PAGE):
            o = self.order(r["id"])
            rows.append([(f"#{o['id']} {KIND_NAME[o['kind']]} · {stars(o['price'])} · {STATUS_NAME[o['status']]}",
                          f"o:{o['id']}")])
        return f"📋 <b>Все заказы</b>: {total}", kb(*rows, *self.pager("ol", page, total * 2, "shop"))

    async def c_o(self, uid, cq, oid):
        o = self.order(int(oid))
        if not o:
            return await self.c_oq(uid, cq)
        st, i = o["status"], o["id"]
        rows = []
        if st == "review":
            rows.append([("✅ Одобрить", f"oa:{i}"), ("❌ Отклонить", f"orj:{i}")])
        if st == "approved":
            rows.append([("❌ Отменить (не оплачен)", f"orj:{i}")])
        if o["kind"] == "pin" and o["data"].get("content"):
            rows.append([("👁 Показать пост", f"opv:{i}")])
        if st == "live" and o["kind"] != "vip":
            rows.append([("⏹ Завершить сейчас", f"oe:{i}")])
        if st in ("paid", "live") and o["charge"]:
            rows.append([("↩️ Вернуть звёзды", f"orf:{i}")])
        if o["kind"] == "op" and o["data"].get("channel") and st == "live":
            rows.append([("📢 Канал в ОП", f"oc:{o['data']['channel']}")])
        rows.append([("🧾 Заявки", "oq"), ("👤 Покупатель", f"u:{o['uid']}")])
        return self.order_text(o, admin=True), kb(*rows)

    async def c_oa(self, uid, cq, oid):
        o = self.order(int(oid))
        if not o or o["status"] != "review":
            return "Заявку уже обработали"
        self.save_order(o, status="approved")
        await self.send(o["uid"], f"✅ Заказ #{o['id']} одобрен! Осталось оплатить 👇")
        await self.invoice(o)
        t, mk = await self.c_o(uid, cq, oid)
        return t, mk, "Одобрено, счёт отправлен"

    async def c_orj(self, uid, cq, oid):
        await self.ask(uid, {"k": "reject", "oid": int(oid)}, f"❌ Заказ #{oid}: напиши причину отказа — её увидит покупатель.",
                       f"o:{oid}", [[("Отклонить без причины", f"orjn:{oid}")]])
        return ""

    async def reject(self, uid, oid, why=""):
        o = self.order(int(oid))
        if not o or o["status"] not in ("review", "approved"):
            return "Заказ уже не в проверке"
        self.save_order(o, status="rejected", note=why)
        await self.send(o["uid"], f"❌ Заказ #{o['id']} отклонён." + (f"\nПричина: {esc(why)}" if why else "") +
                        "\nМожно оформить заново: /start")
        return ""

    async def c_orjn(self, uid, cq, oid):
        err = await self.reject(uid, oid)
        t, mk = await self.c_o(uid, cq, oid)
        return t, mk, err or "Отклонено"

    async def s_reject(self, m, st, text):
        uid = m["from"]["id"]
        self.steps.pop(uid, None)
        err = await self.reject(uid, st["oid"], text[:500])
        if err:
            await self.send(uid, err)
        await self.show(uid, *await self.c_o(uid, None, st["oid"]))

    async def c_opv(self, uid, cq, oid):
        o = self.order(int(oid))
        ct = o and o["data"].get("content")
        if not ct:
            return "Поста нет"
        await self.send_content(uid, ct, {"inline_keyboard": o["data"]["btns"]} if o["data"].get("btns") else None)
        return "Пост ниже 👇"

    async def c_oe(self, uid, cq, oid):
        return (f"⏹ Завершить заказ #{oid} прямо сейчас? Звёзды не возвращаются (для этого — «Вернуть звёзды»).",
                kb([("⏹ Завершить", f"oee:{oid}")], [("⬅️ Нет", f"o:{oid}")]))

    async def c_oee(self, uid, cq, oid):
        o = self.order(int(oid))
        if o and o["status"] == "live":
            o["end"] = self.now()
            await self.finish(o)
        return await self.c_o(uid, cq, oid)

    async def c_orf(self, uid, cq, oid):
        o = self.order(int(oid))
        return (f"↩️ Вернуть {stars(o['price'])} покупателю и остановить заказ #{oid}?",
                kb([("↩️ Да, вернуть", f"orff:{oid}")], [("⬅️ Нет", f"o:{oid}")]))

    async def c_orff(self, uid, cq, oid):
        o = self.order(int(oid))
        if not o or o["status"] not in ("paid", "live"):
            return "Уже не активен"
        err = await self.refund(o, "Возврат сделал владелец.")
        t, mk = await self.c_o(uid, cq, oid)
        return t, mk, err or "Звёзды возвращены"

    # ───────── таймеры ─────────
    async def tick(self):
        now = self.now()
        parts = [self.tick_trash, self.tick_captcha, self.tick_orders, self.tick_posts, self.tick_locks]
        minute, hour = int(now // 60), int(now // 3600)
        if minute != self.marks["min"]:
            self.marks["min"] = minute
            parts.append(self.tick_night)
        if hour != self.marks["hour"]:
            self.marks["hour"] = hour
            parts.append(self.hourly)
        for f in parts:
            try:
                await f()
            except Exception:
                log.exception("tick %s", f.__name__)

    async def tick_trash(self):
        rows = self.db.q("SELECT rowid AS r, chat, mid FROM trash WHERE at<=? LIMIT 300", self.now())
        by = {}
        for r in rows:
            self.db.x("DELETE FROM trash WHERE rowid=?", r["r"])
            by.setdefault(r["chat"], []).append(r["mid"])
        for cid, mids in by.items():
            if len(mids) == 1:
                await self.delete(cid, mids[0])
            else:
                await self.q("deleteMessages", chat_id=cid, message_ids=mids[:100])

    async def tick_captcha(self):
        for r in self.db.q("SELECT * FROM captcha WHERE deadline<=?", self.now()):
            c = self.chat(r["chat"])
            if c:
                await self.captcha_done(c, r, False)
            else:
                self.db.x("DELETE FROM captcha WHERE chat=? AND uid=?", r["chat"], r["uid"])

    async def tick_orders(self):
        now = self.now()
        for r in self.db.q("SELECT id FROM orders WHERE status='paid' AND start<=?", now):
            o = self.order(r["id"])
            if o["data"].get("retry", 0) <= now:
                await self.go_live(o)
        for r in self.db.q("SELECT id FROM orders WHERE status='live' AND end<=?", now):
            await self.finish(self.order(r["id"]))

    async def tick_posts(self):
        for r in self.db.q("SELECT id FROM posts WHERE next>0 AND next<=?", self.now()):
            p = self.post(r["id"])
            if p["s"]["p_on"] and p["content"]:
                await self.send_post(p)
            else:
                self.db.x("UPDATE posts SET next=0 WHERE id=?", p["id"])

    async def tick_locks(self):
        for c in self.db.q("SELECT id FROM chats WHERE night IS NOT NULL AND active=1"):
            c = self.chat(c["id"])
            st = json.loads(c["night"])
            if st.get("why") == "ro" and st.get("until") and st["until"] <= self.now():
                await self.unlock(c)

    @staticmethod
    def is_night(s, h):
        a, b = s["night_from"], s["night_to"]
        if a == b:
            return False
        return a <= h < b if a < b else (h >= a or h < b)

    async def tick_night(self):
        h = self.local().hour
        for c in self.chats():
            st = json.loads(c["night"]) if c["night"] else None
            night = c["s"]["night_on"] and self.is_night(c["s"], h)
            if night and not st:
                await self.lock(c, "night")
            elif not night and st and st.get("why") == "night":
                await self.unlock(c)

    async def hourly(self):
        now = self.now()
        for c in self.chats():
            n = await self.q("getChatMemberCount", chat_id=c["id"])
            if n:
                self.db.x("UPDATE chats SET members=? WHERE id=?", n, c["id"])
        for d, ttl in ((self.notice_t, 3600), (self.recent_join, 120), (self.recent_left, 120), (self.subcache, SUB_CACHE)):
            for k in [k for k, t in d.items() if now - t > ttl]:
                d.pop(k, None)
        for k in [k for k, v in self.op_missing.items() if now - v[0] > 3600]:
            self.op_missing.pop(k, None)
        for k in [k for k, q in self.flood.items() if not q or now - q[-1] > 120]:
            self.flood.pop(k, None)
        self.albums.clear()
        self.flood_mg.clear()
        for k in [k for k, lk in self.locks.items() if not lk.locked()]:
            self.locks.pop(k, None)
        self.db.x("DELETE FROM orders WHERE status='draft' AND created<?", now - 86400)
        for r in self.db.q("SELECT id FROM orders WHERE status='approved' AND created<?", now - 3 * 86400):
            o = self.order(r["id"])
            self.save_order(o, status="cancelled", note="не оплачен за 3 дня")
            await self.send(o["uid"], f"⌛ Заказ #{o['id']} отменён: не оплачен за 3 дня. Оформить заново — /start")
        self.db.x("DELETE FROM vip WHERE until>0 AND until<?", now - 86400)
        self.db.x("DELETE FROM log WHERE ts<?", now - 180 * 86400)
        self.db.x("DELETE FROM stats WHERE day<?", (self.local() - timedelta(days=400)).date().isoformat())
        day = self.day()
        if self.local().hour >= 4 and self.db.get("backup_day") != day:
            self.db.set("backup_day", day)
            if self.cfg["backup_daily"] and self.staff("owner"):
                await self.backup()

    async def ticker(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick")
            await asyncio.sleep(TICK)

    # ───────── запуск ─────────
    async def start(self):
        self.me = await self.api("getMe")
        self.me_id, self.username = self.me["id"], self.me.get("username") or "bot"
        await self.q("deleteWebhook", drop_pending_updates=False)
        await self.q("setMyCommands", scope={"type": "all_private_chats"}, commands=[
            {"command": "start", "description": "Реклама в чатах (для владельца — панель)"},
            {"command": "orders", "description": "Мои заказы"}])
        await self.q("setMyCommands", scope={"type": "all_group_chats"}, commands=[
            {"command": "rules", "description": "Правила чата"},
            {"command": "report", "description": "Пожаловаться (ответом на сообщение)"}])
        await self.q("setMyCommands", scope={"type": "all_chat_administrators"}, commands=[
            {"command": c, "description": d} for c, d in (
                ("ban", "Бан [срок] [причина]"), ("mute", "Мут [срок] [причина]"), ("warn", "Варн [причина]"),
                ("kick", "Выкинуть"), ("unban", "Разбан"), ("unmute", "Размут"), ("unwarn", "Снять варн"),
                ("warns", "Варны человека"), ("del", "Удалить сообщение"), ("purge", "Удалить всё от этого сообщения"),
                ("pin", "Закрепить"), ("unpin", "Открепить"), ("ro", "Закрыть чат [срок] / off"),
                ("cd", "Кулдаун [срок]"), ("info", "Карточка человека"), ("rules", "Правила"), ("help", "Все команды"))])
        if not self.db.get("descr_set"):
            await self.q("setMyShortDescription", short_description="Модератор наших чатов. Здесь можно купить рекламу за ⭐")
            await self.q("setMyDescription", description="🛡 Модератор чатов.\n\n📣 Здесь можно купить рекламу в наших "
                                                         "чатах: канал в обязательную подписку, закреп или VIP — "
                                                         "за звёзды Telegram. Жми «Старт».")
            self.db.set("descr_set", True)
        log.info("Метроном · Страж %s: @%s, чатов %d", VERSION, self.username, len(self.chats()))

    def lock_key(self, up):
        for k in ("message", "edited_message", "callback_query", "my_chat_member", "chat_member", "pre_checkout_query"):
            if k in up:
                x = up[k]
                return (x.get("from") or {}).get("id") or (x.get("chat") or {}).get("id") or 0
        return 0

    async def dispatch(self, up):
        lk = self.locks.setdefault(self.lock_key(up), asyncio.Lock())
        async with lk:
            await self.on_update(up)

    async def run(self):
        while True:
            try:
                await self.start()
                break
            except ApiError as e:
                if e.code == 401:
                    log.error("Telegram не принял BOT_TOKEN — проверь config.env")
                    raise SystemExit(1)
                log.warning("getMe: %s — повторю через 10 с", e.desc)
            except Exception as e:
                log.warning("Нет связи с Telegram (%r) — повторю через 10 с", e)
            await asyncio.sleep(10)
        asyncio.ensure_future(self.ticker())
        offset = self.db.get("offset", 0)
        while True:
            try:
                ups = await self.api("getUpdates", offset=offset, timeout=50, allowed_updates=UPDATES)
            except ApiError as e:
                if e.code == 401:
                    log.error("Telegram не принял BOT_TOKEN — проверь config.env")
                    raise SystemExit(1)
                if e.code == 409:
                    log.error("Этот токен уже используется другим запуском бота (или вебхуком). Жду…")
                    await asyncio.sleep(15)
                else:
                    await asyncio.sleep(3)
                continue
            except Exception as e:
                log.warning("getUpdates: %r", e)
                await asyncio.sleep(3)
                continue
            for up in ups:
                offset = up["update_id"] + 1
                asyncio.ensure_future(self.dispatch(up))
            if ups:
                self.db.set("offset", offset)


def reset_password_from_env(db, env):
    """NEW_PASSWORD в настройках сервера (Render) — сменить пароль панели один раз, без консоли."""
    new = (env.get("NEW_PASSWORD") or "").strip()
    if not new:
        return False
    if len(new) < 8:
        log.warning("NEW_PASSWORD короче 8 символов — пароль не сменён")
        return False
    mark = hashlib.sha256(("metronom:" + new).encode()).hexdigest()
    if db.get("pw_reset") == mark:
        return False
    db.set("pw", hash_pw(new))
    db.x("DELETE FROM staff WHERE role='owner'")
    db.set("pw_reset", mark)
    log.info("Пароль панели сменён (NEW_PASSWORD), все владельцы вышли. Войди заново: /login")
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = load_env(os.path.join(HERE, "config.env"))
    for k in ("BOT_TOKEN", "PASSWORD", "NEW_PASSWORD", "TZ_NAME", "DB", "DB_URL", "DB_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    db_url = (env.get("DB_URL") or "").strip()
    try:
        db = DB(db_url or env.get("DB") or os.path.join(HERE, "guard.db"), env.get("DB_TOKEN"))
    except Exception as e:
        sys.exit("База не открылась: %s" % e)
    log.info("база: %s", "облако Turso" if db_url else "файл")
    reset_password_from_env(db, env)
    if len(sys.argv) > 2 and sys.argv[1] == "--password":
        if len(sys.argv[2]) < 8:
            sys.exit("Пароль — от 8 символов.")
        db.set("pw", hash_pw(sys.argv[2]))
        db.x("DELETE FROM staff WHERE role='owner'")
        print("Пароль сменён, все владельцы вышли из панели. Перезапусти бота и войди: /login")
        return
    if not env.get("BOT_TOKEN"):
        sys.exit("Нет BOT_TOKEN — впиши его в config.env рядом с guard.py")
    g = Guard(BotAPI(env["BOT_TOKEN"]), db, env)
    tz = env.get("TZ_NAME") or env.get("TZ")
    if tz and not (db.get("cfg") or {}).get("tz") and tz in TZ_FALLBACK:
        g.cfg["tz"] = tz
        g.save_cfg()
    try:
        asyncio.run(g.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
