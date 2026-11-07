"""Метроном · личный сервер пользователя (Render, бесплатно).

Здесь, на странице сервера, человек входит в свой Telegram. Дальше всё управление — в общем боте:
бот присылает сюда команды по защищённому ключу, сервер сам рассылает рекламу от аккаунта.

Вход хранится только тут (в памяти). Боту отдаётся зашифрованная копия для перезапусков —
расшифровать её можно только ключом NODE_SECRET, который лежит в настройках Render у самого пользователя.
"""
import asyncio
import base64
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone

import segno
from aiohttp import ClientSession, ClientTimeout, web
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from telethon import TelegramClient, errors, types, utils
from telethon.sessions import StringSession

import main as core

VERSION = "2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
KEEPALIVE = 9 * 60          # сам себя будит, чтобы Render не усыпил (засыпает после 15 мин тишины)
CODE_LIMIT = (5, 30 * 60)   # не больше 5 запросов кода за 30 минут
QR_TOTAL = 5 * 60           # QR-код живёт 5 минут, потом «обновить»
log = logging.getLogger("node")


# ───────────────────────── настройки и ключи ─────────────────────────

def load_settings():
    s = {}
    try:
        with open(os.path.join(HERE, "settings.json"), encoding="utf-8-sig") as f:
            s = json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    for k, e in (("api_id", "API_ID"), ("api_hash", "API_HASH"), ("hub_bot", "HUB_BOT")):
        if os.environ.get(e):
            s[k] = os.environ[e]
    return s


def node_secret():
    if os.environ.get("NODE_SECRET"):
        return os.environ["NODE_SECRET"]
    p = os.path.join(HERE, ".node_secret")   # не Render: ключ в файле рядом
    try:
        return open(p).read().strip()
    except FileNotFoundError:
        s = secrets.token_urlsafe(32)
        with open(p, "w") as f:
            f.write(s)
        log.warning("NODE_SECRET не задан — создал файл .node_secret")
        return s


def derive(secret):
    k = secret.encode()
    hub_key = hmac.new(k, b"metronom/hub-key", hashlib.sha256).hexdigest()[:40]
    blob_key = hmac.new(k, b"metronom/blob-key", hashlib.sha256).digest()
    return hub_key, blob_key


def seal(key, obj):
    n = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(n, json.dumps(obj, ensure_ascii=False).encode(), b"metronom-v1")
    return base64.urlsafe_b64encode(n + ct).decode()


def unseal(key, s):
    try:
        raw = base64.urlsafe_b64decode(s.encode())
        return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], b"metronom-v1"))
    except Exception as e:
        raise ValueError("bad blob") from e


# ───────────────────────── разметка Bot API ↔ Telethon ─────────────────────────

_B2T = {"bold": "Bold", "italic": "Italic", "underline": "Underline", "strikethrough": "Strike", "spoiler": "Spoiler",
        "code": "Code", "pre": "Pre", "text_link": "TextUrl", "url": "Url", "mention": "Mention", "hashtag": "Hashtag",
        "cashtag": "Cashtag", "email": "Email", "phone_number": "Phone", "bot_command": "BotCommand",
        "custom_emoji": "CustomEmoji", "blockquote": "Blockquote", "expandable_blockquote": "Blockquote"}
_T2B = {v: k for k, v in _B2T.items() if k != "expandable_blockquote"}


def ents_from_bot(lst, custom_emoji=True):
    out = []
    for e in lst or []:
        k = _B2T.get(e.get("type"))
        if not k or (k == "CustomEmoji" and not custom_emoji):
            continue
        o, n = int(e["offset"]), int(e["length"])
        if k == "TextUrl":
            out.append(types.MessageEntityTextUrl(o, n, e.get("url") or ""))
        elif k == "Pre":
            out.append(types.MessageEntityPre(o, n, e.get("language") or ""))
        elif k == "CustomEmoji":
            out.append(types.MessageEntityCustomEmoji(o, n, int(e["custom_emoji_id"])))
        elif k == "Blockquote":
            out.append(types.MessageEntityBlockquote(o, n, collapsed=e["type"] == "expandable_blockquote" or None))
        else:
            out.append(getattr(types, "MessageEntity" + k)(o, n))
    return out


def ents_to_bot(lst):
    out = []
    for e in lst or []:
        k = type(e).__name__.replace("MessageEntity", "")
        t = _T2B.get(k)
        if not t or t == "custom_emoji":      # боты не могут показывать премиум-эмодзи
            continue
        if t == "blockquote" and getattr(e, "collapsed", None):
            t = "expandable_blockquote"
        d = {"type": t, "offset": e.offset, "length": e.length}
        if t == "text_link":
            d["url"] = e.url
        if t == "pre" and getattr(e, "language", ""):
            d["language"] = e.language
        out.append(d)
    return out


# ───────────────────────── рассылка (ядро Метронома) ─────────────────────────

class NodeAutopost(core.Autopost):
    """Тот же Метроном, но настройки живут в памяти + зашифрованной копией у бота, сообщения — в бот."""

    def __init__(self, client, node, premium=False):
        super().__init__(client, data_path=os.devnull, premium=premium, gap=node.gap)
        self.node = node
        self.st.update(sent_total=0, ad_meta=None)

    def _apply(self, d):
        super()._apply(d)
        self.st["sent_total"] = int(d.get("sent_total", 0))
        self.st["ad_meta"] = d.get("ad_meta")

    def save(self):
        self.node.touch()

    async def backup(self):
        pass

    async def restore(self):
        return False

    async def refresh_footer(self):
        self.footer_checked = self.now()
        return True

    async def say(self, text):
        self.node.notify(text)

    def footer(self):
        f = self.st.get("footer") or {"text": self.node.default_footer, "ents": []}
        if f.get("ad_until") and datetime.now(timezone.utc).date().isoformat() > f["ad_until"]:
            base = f.get("base") or self.node.default_footer
            return {"text": base, "ents": [e for e in f.get("ents", []) if e["o"] + e["l"] <= core.u16(base)]}
        return f

    async def send_one(self, c, ad):
        r = await super().send_one(c, ad)
        if r == "ok":
            self.st["sent_total"] = self.st.get("sent_total", 0) + 1
        return r


# ───────────────────────── сервер ─────────────────────────

class Node:
    def __init__(self, settings, secret, make_client=None, public_url="", sleep=asyncio.sleep):
        # ключи Telegram клиент вписывает сам на странице сервера; API_ID/API_HASH в окружении — необязательно
        self.api_id = int(re.sub(r"\D", "", str(settings.get("api_id") or "")) or 0)
        self.api_hash = str(settings.get("api_hash") or "").strip()
        self.api_fixed = bool(self.api_id and self.api_hash)
        self.hub_bot = str(settings.get("hub_bot") or "").lstrip("@")
        self.hub_key, self.blob_key = derive(secret)
        self.public_url = public_url.rstrip("/")
        self.make_client = make_client or self._client
        self.sleep = sleep
        self.gap = core.GAP
        self.state = "new"            # new — не вошли · running — работает · relogin — вход слетел
        self.client = self.bot = self.me = self.owner = None
        self.footer = None
        self.last_settings = None
        self.pending = None
        self.outbox, self.out_id = [], 0
        self.boot = secrets.token_hex(3)
        self.n = 0
        self.error = ""
        self.pair_error = ""
        self.sched = None
        self.groups_cache = []
        self.code_times = []
        self.bg = set()

    @property
    def default_footer(self):
        return f"⏱ Отправлено через Метроном — бесплатная авторассылка: @{self.hub_bot}" if self.hub_bot \
            else core.DEFAULT_FOOTER

    @property
    def ver(self):
        return f"{self.boot}:{self.n}"

    def _client(self, session, api_id=None, api_hash=None):
        return TelegramClient(StringSession(session or None), api_id or self.api_id, api_hash or self.api_hash,
                              device_model="Metronom",
                              system_version="Cloud", app_version=VERSION, flood_sleep_threshold=60)

    def touch(self):
        self.n += 1

    def notify(self, text):
        self.out_id += 1
        self.outbox.append({"id": self.out_id, "text": text[:3900]})
        self.outbox = self.outbox[-30:]

    def task(self, coro):
        t = asyncio.ensure_future(coro)
        self.bg.add(t)
        t.add_done_callback(self.bg.discard)
        return t

    def me_info(self):
        if not self.me:
            return None
        return {"id": self.me.id, "name": utils.get_display_name(self.me) or "", "username": self.me.username}

    def blob(self):
        return seal(self.blob_key, {"v": 2, "session": self.client.session.save(), "owner": self.owner,
                                    "settings": self.bot.st, "footer": self.footer,
                                    "api": [self.api_id, self.api_hash] if self.api_id and self.api_hash else None})

    def set_footer(self, f):
        if isinstance(f, dict) and isinstance(f.get("text"), str) and 0 < len(f["text"]) <= 600:
            f = {"text": f["text"], "ents": [e for e in f.get("ents") or [] if isinstance(e, dict)],
                 "ad_until": f.get("ad_until"), "base": f.get("base")}
            if f != self.footer:
                self.footer = f
                if self.bot:
                    self.bot.st["footer"] = f
                    self.touch()

    # ── запуск рассылки ──
    async def start_bot(self, client, me, settings=None):
        self.client, self.me, self.owner = client, me, me.id
        try:
            await client.get_dialogs()      # чтобы Telegram «узнал» чаты после перезапуска
        except Exception as e:
            log.warning("get_dialogs: %s", e)
        bot = NodeAutopost(client, self, premium=bool(getattr(me, "premium", False)))
        if settings:
            bot._apply(settings)
        if self.footer:
            bot.st["footer"] = self.footer
        self.bot = bot
        self.state = "running"
        self.error = ""
        self.touch()
        self.sched = self.task(self._scheduler())
        log.info("работаю за %s (id %s), чатов %d", utils.get_display_name(me), me.id, len(bot.st["chats"]))

    async def _scheduler(self):
        try:
            await self.bot.scheduler()
        except asyncio.CancelledError:
            raise
        except core.FATAL as e:
            log.error("сессия отключена: %s", e.__class__.__name__)
            self.notify("⏱ ⛔ Telegram отключил вход на сервере (вышли из сессии или аккаунт ограничен). "
                        "Рассылка остановлена — открой свой сервер и войди заново: " + self.public_url)
            await self.drop("relogin")
        except Exception:
            log.exception("планировщик упал")
            self.sched = self.task(self._scheduler()) if self.state == "running" else None

    async def drop(self, state):
        if self.sched and self.sched is not asyncio.current_task():
            self.sched.cancel()
        self.sched = None
        if self.bot:
            self.last_settings = dict(self.bot.st)
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = self.bot = None
        self.state = state

    # ── вход ──
    async def _cancel_pending(self):
        p, self.pending = self.pending, None
        if p:
            if p.get("task"):
                p["task"].cancel()
            try:
                await p["client"].disconnect()
            except Exception:
                pass

    def pick_api(self, api_id=None, api_hash=None):
        """Ключи из формы (если вписаны) или уже сохранённые на сервере."""
        aid = re.sub(r"\s", "", str(api_id or ""))
        ah = re.sub(r"\s", "", str(api_hash or "")).lower()
        if (aid or ah) and not self.api_fixed:
            if not re.fullmatch(r"\d{3,12}", aid):
                raise LoginError("api_id — это только цифры (строка «App api_id» на my.telegram.org).")
            if not re.fullmatch(r"[0-9a-f]{32}", ah):
                raise LoginError("api_hash — 32 символа, цифры и буквы a–f (строка «App api_hash» на my.telegram.org).")
            return int(aid), ah
        if self.api_id and self.api_hash:
            return self.api_id, self.api_hash
        raise LoginError("Впиши api_id и api_hash — инструкция на этой странице, займёт 2 минуты на my.telegram.org.")

    async def _new_pending(self, kind, api_id=None, api_hash=None):
        api = self.pick_api(api_id, api_hash)
        await self._cancel_pending()
        c = self.make_client("", *api)
        await c.connect()
        self.pending = {"client": c, "kind": kind, "step": None, "phone": None, "qr": None, "svg": "", "qv": 0,
                        "task": None, "since": time.time(), "tries": 0, "api": api}
        self.error = ""
        return self.pending

    def can_login(self):
        return self.state in ("new", "relogin")

    async def login_phone(self, phone, api_id=None, api_hash=None):
        phone = "+" + re.sub(r"\D", "", phone or "")
        if not 11 <= len(phone) <= 16:
            raise LoginError("Номер целиком, с кодом страны: +7 999 123-45-67")
        now = time.time()
        self.code_times = [t for t in self.code_times if now - t < CODE_LIMIT[1]]
        if len(self.code_times) >= CODE_LIMIT[0]:
            raise LoginError("Слишком много запросов кода. Подожди полчаса.")
        p = await self._new_pending("phone", api_id, api_hash)
        try:
            await p["client"].send_code_request(phone)
        except errors.PhoneNumberInvalidError:
            raise LoginError("Telegram не принял номер — проверь цифры.")
        except errors.PhoneNumberBannedError:
            raise LoginError("Этот номер заблокирован в Telegram.")
        except errors.FloodWaitError as e:
            raise LoginError(f"Telegram просит подождать {core.fmt_dur(e.seconds)} перед новым кодом.")
        self.code_times.append(now)
        p.update(phone=phone, step="code")
        return {"step": "code"}

    async def login_code(self, code):
        p = self.pending
        if not p or p["step"] != "code":
            raise LoginError("Сначала введи номер.")
        code = re.sub(r"\D", "", code or "")
        p["tries"] += 1
        if p["tries"] > 8:
            await self._cancel_pending()
            raise LoginError("Слишком много попыток — запроси код заново.")
        try:
            await p["client"].sign_in(phone=p["phone"], code=code)
        except errors.SessionPasswordNeededError:
            p["step"] = "password"
            return {"step": "password"}
        except errors.PhoneCodeInvalidError:
            raise LoginError("Неверный код. Он пришёл в Telegram, в чат «Telegram».")
        except errors.PhoneCodeExpiredError:
            await self._cancel_pending()
            raise LoginError("Код устарел — запроси новый.")
        return await self._finish(p)

    async def login_password(self, pw):
        p = self.pending
        if not p or p["step"] != "password":
            raise LoginError("Пароль сейчас не нужен.")
        p["tries"] += 1
        if p["tries"] > 12:
            await self._cancel_pending()
            raise LoginError("Слишком много попыток. Начни заново.")
        try:
            await p["client"].sign_in(password=pw or "")
        except errors.PasswordHashInvalidError:
            raise LoginError("Неверный облачный пароль.")
        return await self._finish(p)

    async def login_qr(self, api_id=None, api_hash=None):
        p = await self._new_pending("qr", api_id, api_hash)
        p["qr"] = await p["client"].qr_login()
        p["svg"] = qr_svg(p["qr"].url)
        p["step"] = "qr"
        p["task"] = self.task(self._qr_wait(p))
        return {"step": "qr"}

    async def _qr_wait(self, p):
        try:
            while self.pending is p and time.time() - p["since"] < QR_TOTAL:
                left = (p["qr"].expires - datetime.now(timezone.utc)).total_seconds()
                try:
                    await p["qr"].wait(timeout=max(3.0, left))
                except asyncio.TimeoutError:
                    await p["qr"].recreate()
                    p["svg"], p["qv"] = qr_svg(p["qr"].url), p["qv"] + 1
                    continue
                except errors.SessionPasswordNeededError:
                    p["step"] = "password"
                    return
                p["step"] = "done"
                await self._finish(p)
                return
            if self.pending is p:
                p["step"] = "qr_old"
        except asyncio.CancelledError:
            raise
        except LoginError as e:
            self.error = str(e)
        except Exception as e:
            log.exception("QR")
            self.error = f"QR-вход не удался: {e.__class__.__name__}. Попробуй по номеру."
            if self.pending is p:
                p["step"] = "qr_old"

    async def _finish(self, p):
        c = p["client"]
        me = await c.get_me()
        if self.owner and me.id != self.owner:
            try:
                await c.log_out()
            except Exception:
                pass
            self.pending = None
            raise LoginError("Этот сервер привязан к другому аккаунту Telegram. Войди им или создай новый сервер.")
        settings = self.last_settings if self.owner == me.id else None
        self.pending = None
        if p.get("api"):
            self.api_id, self.api_hash = p["api"]       # запомнить ключи (и в зашифрованной копии)
        await self.start_bot(c, me, settings)
        await self.pair()
        return {"step": "done"}

    async def pair(self):
        """Сообщить боту адрес и ключ — сообщением от твоего аккаунта (так бот точно знает, чей это сервер)."""
        self.pair_error = ""
        if not self.hub_bot:
            self.pair_error = "В settings.json не указан бот (hub_bot)."
            return False
        if not self.public_url:
            self.pair_error = "Не знаю свой адрес — открой страницу сервера ещё раз."
            return False
        try:
            m = await self.client.send_message(self.hub_bot, f"/connect {self.public_url} {self.hub_key}")
            self.task(self._unsend(m))
            return True
        except Exception as e:
            self.pair_error = f"Не смог написать боту @{self.hub_bot}: {e.__class__.__name__}"
            log.warning(self.pair_error)
            return False

    async def _unsend(self, m):
        await self.sleep(8)
        try:
            await self.client.delete_messages(self.hub_bot, [m.id], revoke=True)
        except Exception:
            pass

    # ── API для бота ──
    def stats(self):
        b = self.bot
        if not b:
            return None
        st = b.st
        nxt = [c.get("next", 0) for c in st["chats"] if c["on"]]
        return {"enabled": st["enabled"], "interval": st["interval"], "chats": len(st["chats"]),
                "on": sum(1 for c in st["chats"] if c["on"]), "sent_total": st.get("sent_total", 0),
                "has_ad": bool(st["ad"]), "next_in": max(0, int(min(nxt) - b.now())) if nxt and st["enabled"] else None,
                "pause": max(0, int(b.pause_until - b.now()))}

    def ping_resp(self, hub_ver=None):
        r = {"ok": True, "v": VERSION, "state": self.state, "owner": self.owner, "me": self.me_info(),
             "ver": self.ver, "stats": self.stats(), "outbox": self.outbox[:20]}
        if self.state == "running" and hub_ver != self.ver:
            r["blob"] = self.blob()
        return r

    async def api_ping(self, b):
        if b.get("footer"):
            self.set_footer(b["footer"])
        ack = int(b.get("ack") or 0)
        self.outbox = [m for m in self.outbox if m["id"] > ack]
        return self.ping_resp(b.get("ver"))

    async def api_restore(self, b):
        if b.get("footer"):
            self.set_footer(b["footer"])
        if self.state == "running":
            return self.ping_resp(b.get("ver"))
        try:
            data = unseal(self.blob_key, b.get("blob") or "")
        except ValueError:
            return {"ok": False, "error": "bad_blob", "state": self.state}
        await self._cancel_pending()
        if not self.footer and data.get("footer"):
            self.footer = data["footer"]
        api = data.get("api")
        if not self.api_fixed and isinstance(api, (list, tuple)) and len(api) == 2 and api[0] and api[1]:
            self.api_id, self.api_hash = int(api[0]), str(api[1])
        if not (self.api_id and self.api_hash):
            self.owner = data.get("owner") or self.owner
            self.last_settings = data.get("settings")
            self.state = "relogin"
            return {"ok": False, "error": "session_dead", "state": self.state, "owner": self.owner}
        c = self.make_client(data["session"], self.api_id, self.api_hash)
        try:
            await c.connect()
            ok = await c.is_user_authorized()
        except Exception as e:
            log.warning("restore connect: %s", e)
            return {"ok": False, "error": "net", "state": self.state}
        self.owner = data.get("owner") or self.owner
        self.last_settings = data.get("settings")
        if not ok:
            await c.disconnect()
            self.state = "relogin"
            return {"ok": False, "error": "session_dead", "state": self.state, "owner": self.owner}
        me = await c.get_me()
        if self.owner and me.id != self.owner:
            await c.disconnect()
            return {"ok": False, "error": "owner_mismatch", "state": self.state}
        await self.start_bot(c, me, data.get("settings"))
        log.info("вход восстановлен ботом")
        return self.ping_resp()

    async def api_do(self, b):
        if self.state != "running":
            return {"ok": False, "error": "not_running", "state": self.state}
        fn = getattr(self, "act_" + str(b.get("action")), None)
        if not fn:
            return {"ok": False, "error": "unknown_action"}
        try:
            res = await fn(b)
        except core.FATAL:
            await self.drop("relogin")
            return {"ok": False, "error": "not_running", "state": self.state}
        res.setdefault("ok", True)
        if self.bot:
            res["stats"] = self.stats()
        return res

    async def act_status(self, b):
        bot, now = self.bot, self.bot.now()
        chats = [{"key": f"{c['id']}:{c.get('topic') or ''}", "title": c["title"], "on": c["on"], "err": c.get("err") or "",
                  "sent": c.get("sent", 0),
                  "next_in": max(0, int(c.get("next", 0) - now)) if c["on"] and bot.st["enabled"] else None}
                 for c in bot.st["chats"]]
        return {"chats": chats, "ad": bot.st.get("ad_meta") if bot.st["ad"] else None, "me": self.me_info()}

    async def act_groups(self, b):
        gs = await self.bot._groups()
        self.groups_cache = gs
        added = {c["id"] for c in self.bot.st["chats"]}
        return {"groups": [dict(g, added=g["id"] in added) for g in gs[:400]]}

    async def act_add(self, b):
        bot, added, bad = self.bot, [], []
        ids = [int(i) for i in b.get("ids") or []]
        if ids:
            known = {g["id"]: g for g in self.groups_cache}
            if any(i not in known for i in ids):
                self.groups_cache = await bot._groups()
                known = {g["id"]: g for g in self.groups_cache}
            for i in ids:
                g = known.get(i)
                if g:
                    added.append(bot._add(g["id"], g["title"], g["username"], None))
                else:
                    bad.append(f"{i} — нет среди твоих групп")
        for tok in re.split(r"[\s,]+", b.get("link") or ""):
            if not tok:
                continue
            t = core.parse_target(tok)
            if not t:
                bad.append(f"{tok} — нужна ссылка t.me/chat или @chat")
                continue
            try:
                e = await self.client.get_entity(t[0])
            except Exception as ex:
                bad.append(f"{tok} — не нашёл ({ex.__class__.__name__}). Сначала вступи в чат.")
                continue
            title = getattr(e, "title", None) or utils.get_display_name(e) or str(t[0])
            added.append(bot._add(utils.get_peer_id(e), title, getattr(e, "username", None), t[1]))
        added = [a for a in added if a]
        if added:
            await bot.changed()
        return {"added": added, "bad": bad}

    def _match(self, keys):
        keys = set(keys or [])
        return [c for c in self.bot.st["chats"] if f"{c['id']}:{c.get('topic') or ''}" in keys]

    async def act_del(self, b):
        bot = self.bot
        if b.get("all"):
            gone = [c["title"] for c in bot.st["chats"]]
            bot.st["chats"] = []
        else:
            ids = {int(i) for i in b.get("ids") or []}
            rm = self._match(b.get("keys")) + [c for c in bot.st["chats"] if c["id"] in ids and not c.get("topic")]
            gone = [c["title"] for c in rm]
            bot.st["chats"] = [c for c in bot.st["chats"] if not any(c is r for r in rm)]
        await bot.changed()
        return {"removed": gone}

    async def act_chat_on(self, b):
        cs = self._match(b.get("keys")) if b.get("keys") else [c for c in self.bot.st["chats"] if not c["on"]]
        for c in cs:
            if b.get("on", True):
                c.update(on=True, err="", fails=0, next=0.0)
            else:
                c.update(on=False, err="выключен вручную")
        await self.bot.changed()
        return {"changed": [c["title"] for c in cs]}

    async def act_interval(self, b):
        bot = self.bot
        sec = max(core.MIN_INTERVAL, int(b.get("sec") or 0))
        old, bot.st["interval"] = bot.st["interval"], sec
        for c in bot.st["chats"]:
            if c.get("next", 0) > bot.now():
                c["next"] = max(bot.now(), c["next"] - old + sec)
        await bot.changed()
        return {"interval": sec}

    async def act_on(self, b):
        bot = self.bot
        if not bot.st["ad"]:
            return {"ok": False, "error": "no_ad"}
        if not any(c["on"] for c in bot.st["chats"]):
            return {"ok": False, "error": "no_chats"}
        bot.st["enabled"] = True
        await bot.changed()
        return {}

    async def act_off(self, b):
        self.bot.st["enabled"] = False
        await self.bot.changed()
        return {}

    async def act_now(self, b):
        bot = self.bot
        if not bot.st["ad"]:
            return {"ok": False, "error": "no_ad"}
        n_on = sum(1 for c in bot.st["chats"] if c["on"])
        if not n_on:
            return {"ok": False, "error": "no_chats"}

        async def go():
            n = await bot.run_due(force=True)
            self.notify(f"⏱ Готово: отправлено {n} из {n_on}." + ("" if n == n_on else " Что не ушло — смотри «Чаты»."))
        self.task(go())
        return {"count": n_on}

    def _preview(self, ad):
        text, ents, media, _ = self.bot.compose(ad)
        return {"text": text, "entities": ents_to_bot(ents), "media": media is not None,
                "fits": self.bot.footer()["text"] in text}

    async def act_test(self, b):
        ad = await self.bot.get_ad()
        if ad is None:
            return {"ok": False, "error": "no_ad"}
        msg = self.bot.compose(ad)
        await self.bot._send("me", msg)
        return {"preview": self._preview(ad)}

    async def act_ad(self, b):
        text = str(b.get("text") or "")
        photo = b.get("photo")
        if not text.strip() and not photo:
            return {"ok": False, "error": "empty"}
        for ce in (True, False):           # премиум-эмодзи без Premium не отправятся — тогда без них
            ents = ents_from_bot(b.get("entities"), custom_emoji=ce)
            try:
                if photo:
                    f = io.BytesIO(base64.b64decode(photo))
                    f.name = "ad.jpg"
                    msg = await self.client.send_file("me", f, caption=text, formatting_entities=ents, parse_mode=None)
                else:
                    msg = await self.client.send_message("me", text, formatting_entities=ents, parse_mode=None)
                break
            except errors.MediaCaptionTooLongError:
                return {"ok": False, "error": "caption_long"}
            except errors.MessageTooLongError:
                return {"ok": False, "error": "text_long"}
            except errors.RPCError:
                if not ce or not any(isinstance(e, types.MessageEntityCustomEmoji) for e in ents):
                    raise
        bot = self.bot
        bot.st["ad"] = {"msg_id": msg.id}
        bot.st["ad_meta"] = {"kind": "photo" if photo else "text", "preview": text[:300]}
        await bot.changed()
        return {"preview": self._preview(msg)}

    async def act_logout(self, b):
        c = self.client
        await self.drop("new")
        try:
            await c.connect()
            await c.log_out()
        except Exception as e:
            log.warning("log_out: %s", e)
        self.owner = self.me = self.last_settings = None
        return {}


class LoginError(Exception):
    pass


def qr_svg(url):
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#0d1626", light="#ffffff")
    return buf.getvalue().decode()


# ───────────────────────── веб ─────────────────────────

PAGE = open(os.path.join(HERE, "page.html"), encoding="utf-8").read() if os.path.exists(os.path.join(HERE, "page.html")) else ""


def make_app(node):
    app = web.Application(client_max_size=24 * 1024 * 1024)

    def remember_url(req):
        if not node.public_url:
            proto = req.headers.get("X-Forwarded-Proto", req.scheme)
            node.public_url = f"{proto}://{req.host}"

    async def page(req):
        remember_url(req)
        return web.Response(text=PAGE.replace("{{BOT}}", html.escape(node.hub_bot or "")), content_type="text/html")

    async def health(req):
        return web.Response(text="ok")

    async def status(req):
        remember_url(req)
        p = node.pending
        return web.json_response({"state": node.state, "step": p["step"] if p else None, "qv": p["qv"] if p else 0,
                                  "error": node.error, "pair_error": node.pair_error, "me": node.me_info(),
                                  "bot": node.hub_bot, "configured": bool(node.hub_bot),
                                  "has_api": bool(node.api_id and node.api_hash), "api_fixed": node.api_fixed})

    async def qr(req):
        p = node.pending
        if not p or not p.get("svg"):
            raise web.HTTPNotFound()
        return web.Response(text=p["svg"], content_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    async def login(req):
        remember_url(req)
        step = req.match_info["step"]
        if step == "cancel":
            await node._cancel_pending()
            return web.json_response({"ok": True})
        if not node.can_login():
            return web.json_response({"ok": False, "error": "Аккаунт уже подключён."})
        try:
            b = await req.json()
        except Exception:
            b = {}
        try:
            if step == "phone":
                r = await node.login_phone(b.get("phone"), b.get("api_id"), b.get("api_hash"))
            elif step == "code":
                r = await node.login_code(b.get("code"))
            elif step == "password":
                r = await node.login_password(b.get("password"))
            elif step == "qr":
                r = await node.login_qr(b.get("api_id"), b.get("api_hash"))
            else:
                raise web.HTTPNotFound()
        except LoginError as e:
            return web.json_response({"ok": False, "error": str(e)})
        except errors.FloodWaitError as e:
            return web.json_response({"ok": False, "error": f"Telegram просит подождать {core.fmt_dur(e.seconds)}."})
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return web.json_response({"ok": False, "error": "Нет связи с Telegram, попробуй ещё раз через минуту."})
        except errors.RPCError as e:
            log.warning("вход: %s", e)
            if step in ("phone", "qr"):
                await node._cancel_pending()
            if isinstance(e, (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError)):
                return web.json_response({"ok": False, "bad_api": True,
                                          "error": "Telegram не принял api_id / api_hash. Проверь, что скопировал их "
                                                   "с my.telegram.org целиком, без пробелов."})
            return web.json_response({"ok": False, "error": f"Telegram ответил ошибкой: {e.__class__.__name__}"})
        return web.json_response({"ok": True, **r})

    async def api(req):
        if not hmac.compare_digest(req.headers.get("X-Key", ""), node.hub_key):
            return web.json_response({"ok": False, "error": "bad_key"}, status=403)
        try:
            b = await req.json()
        except Exception:
            b = {}
        name = req.match_info["name"]
        fn = {"ping": node.api_ping, "restore": node.api_restore, "do": node.api_do}.get(name)
        if not fn:
            raise web.HTTPNotFound()
        try:
            return web.json_response(await fn(b))
        except Exception as e:
            log.exception("api %s", name)
            return web.json_response({"ok": False, "error": f"{e.__class__.__name__}: {e}"[:300], "state": node.state})

    app.add_routes([web.get("/", page), web.get("/health", health), web.get("/s", status), web.get("/qr.svg", qr),
                    web.post("/login/{step}", login), web.post("/api/{name}", api)])
    return app


async def keepalive(node):
    await asyncio.sleep(60)
    async with ClientSession(timeout=ClientTimeout(total=30)) as s:
        while True:
            if node.public_url.startswith("https://"):
                try:
                    async with s.get(node.public_url + "/health") as r:
                        await r.read()
                except Exception as e:
                    log.info("самопинг: %s", e.__class__.__name__)
            await asyncio.sleep(KEEPALIVE)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%d.%m %H:%M:%S", stream=sys.stdout)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    node = Node(load_settings(), node_secret(), public_url=os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL") or "")
    if not node.hub_bot:
        log.error("В settings.json не указан бот (hub_bot) — сервер не сможет подключиться к боту.")
    app = make_app(node)

    async def on_start(app):
        app["ka"] = asyncio.ensure_future(keepalive(node))
    app.on_startup.append(on_start)
    port = int(os.environ.get("PORT", "10000"))
    log.info("Метроном-сервер %s на порту %s, адрес %s", VERSION, port, node.public_url or "?")
    web.run_app(app, host="0.0.0.0", port=port, print=None, access_log=None)


if __name__ == "__main__":
    main()
