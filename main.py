"""Метроном — бесплатная авторассылка в Telegram-чаты от твоего аккаунта (Telethon). Канал: @metronom_tg

Управление — командами в «Избранном» (Saved Messages) с этого же аккаунта: /help.
Настройки: config.env рядом с файлом (или переменные окружения API_ID, API_HASH, SESSION).
К каждому сообщению добавляется короткая приписка Метронома — так проект остаётся бесплатным.
"""
import asyncio
import io
import json
import logging
import os
import random
import re
import sys
import time
from datetime import date, datetime, timezone

from telethon import TelegramClient, errors, events, types, utils
from telethon.sessions import StringSession

VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
MIN_INTERVAL = 5 * 60          # не чаще раза в 5 минут на чат
GAP = (4.0, 9.0)               # пауза между отправками в разные чаты, сек
MAX_FAILS = 3                  # столько ошибок подряд — чат отключается
BACKUP_TAG = "#metronom_backup"
OLD_BACKUP_TAGS = ("#autopost_backup",)
TICK = 10                      # как часто планировщик проверяет очередь, сек

CHANNEL = "metronom_tg"        # канал проекта: отсюда берётся приписка
FOOTER_TAG = "#metronom"       # пост-«админка» в канале начинается с этой строки
FOOTER_EVERY = 30 * 60         # как часто перечитывать приписку, сек
MAX_FOOTER = 500               # длиннее — игнорируется (защита от ошибки в посте)
DEFAULT_FOOTER = "⏱ Отправлено через Метроном — бесплатная авторассылка: @metronom_tg"
TEXT_LIMIT, CAPTION_LIMIT = 4096, 1024

log = logging.getLogger("metronom")

# ошибки «сюда писать нельзя» — чат отключается сразу, без повторов
FORBIDDEN = tuple(getattr(errors, n) for n in (
    "ChatWriteForbiddenError", "UserBannedInChannelError", "ChannelPrivateError",
    "ChatAdminRequiredError", "ChatRestrictedError", "ChatSendPlainForbiddenError",
    "ChatGuestSendForbiddenError", "ChannelInvalidError", "ChatSendMediaForbiddenError",
    "ForbiddenError", "UserNotParticipantError", "ChatIdInvalidError", "PeerIdInvalidError",
) if hasattr(errors, n))
FATAL = tuple(getattr(errors, n) for n in (
    "AuthKeyUnregisteredError", "SessionRevokedError", "UserDeactivatedBanError",
    "UserDeactivatedError", "AuthKeyDuplicatedError",
) if hasattr(errors, n))


# ───────────────────────── настройки ─────────────────────────

def load_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def get_config():
    cfg = load_env(os.path.join(HERE, "config.env"))
    for k in ("API_ID", "API_HASH", "SESSION"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


# ───────────────────────── разбор команд ─────────────────────────

_UNITS = {"d": 86400, "д": 86400, "h": 3600, "ч": 3600, "m": 60, "м": 60, "мин": 60, "s": 1, "с": 1, "сек": 1}


def parse_interval(s):
    """'30m' '2h' '1h30m' '1ч 30м' '90' (минуты) → секунды или None."""
    s = (s or "").strip().lower().replace(" ", "")
    if not s:
        return None
    if s.isdigit():
        return int(s) * 60
    total, pos = 0, 0
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)(мин|сек|d|д|h|ч|m|м|s|с)", s):
        if m.start() != pos:
            return None
        total += float(m.group(1).replace(",", ".")) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} с"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    parts = []
    if d:
        parts.append(f"{d} д")
    if h:
        parts.append(f"{h} ч")
    if m and not d:
        parts.append(f"{m} мин")
    return " ".join(parts) or "0 мин"


_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,})(?:/(\d+))?/?$")
_CLINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)(?:/(\d+))?(?:/\d+)?/?$")


def parse_target(tok):
    """@user | t.me/user[/topic] | t.me/c/123[/topic] | -100123 → (ref, topic) или None."""
    tok = tok.strip().rstrip(",")
    if tok.startswith("@") and len(tok) > 1:
        return tok[1:], None
    m = _LINK.match(tok)
    if m and m.group(1).lower() not in ("joinchat", "addlist", "c"):
        return m.group(1), int(m.group(2)) if m.group(2) else None
    m = _CLINK.match(tok)
    if m:
        return int("-100" + m.group(1)), int(m.group(2)) if m.group(2) else None
    if re.fullmatch(r"-100\d{5,}", tok):
        return int(tok), None
    return None


# ───────────────────────── приписка ─────────────────────────

def u16(s):
    """Длина в единицах UTF-16 — так Telegram считает лимиты и смещения разметки."""
    return len(s.encode("utf-16-le")) // 2


_ENT_KINDS = ("Bold", "Italic", "Underline", "Strike", "Code", "Spoiler", "TextUrl", "Url", "Mention")


def ent_to_dict(e):
    kind = type(e).__name__.replace("MessageEntity", "")
    if kind not in _ENT_KINDS:
        return None
    d = {"t": kind, "o": e.offset, "l": e.length}
    if kind == "TextUrl":
        d["u"] = e.url
    return d


def ent_from_dict(d, shift=0):
    cls = getattr(types, "MessageEntity" + d["t"])
    if d["t"] == "TextUrl":
        return cls(offset=d["o"] + shift, length=d["l"], url=d["u"])
    return cls(offset=d["o"] + shift, length=d["l"])


_AUTO = re.compile(r"(?:https?://|www\.|t\.me/)[^\s]+|(?<![\w@/.])@[A-Za-z][A-Za-z0-9_]{3,31}\b")


def autolink(text, ents):
    """Добавить ссылки/упоминания, которые не размечены, — чтобы @канал и ссылки были кликабельны."""
    ents = list(ents)
    busy = [(e.offset, e.offset + e.length) for e in ents]
    for m in _AUTO.finditer(text):
        tok = m.group(0)
        if not tok.startswith("@"):
            tok = tok.rstrip(".,!?;:»)\"'")
        a = u16(text[:m.start()])
        b = a + u16(tok)
        if any(x < b and a < y for x, y in busy):
            continue
        cls = types.MessageEntityMention if tok.startswith("@") else types.MessageEntityUrl
        ents.append(cls(offset=a, length=b - a))
        busy.append((a, b))
    return sorted(ents, key=lambda e: e.offset)


def is_footer_post(text):
    first = next((ln.strip() for ln in (text or "").split("\n") if ln.strip()), "")
    return first.lower() == FOOTER_TAG


_UNTIL = re.compile(r"до\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", re.I)


def _until(line, posted):
    m = _UNTIL.search(line)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    base = posted or date.today()
    if y:
        y = int(y) + (2000 if len(y) == 2 else 0)
    else:  # без года: ближайшая такая дата после публикации поста
        y = base.year
        try:
            if date(y, mo, d) < base:
                y += 1
        except ValueError:
            return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_footer(text, entities=(), today=None, posted=None):
    """Пост канала → приписка {"text", "ents", "ad_until"} или None, если это не пост-приписка.

    #metronom
    ⏱ Отправлено через Метроном — бесплатная авторассылка: @metronom_tg   ← основная строка (пусто — по умолчанию)
    #реклама до 20.12                                                       ← платная строка, до какого дня включительно
    🔥 Текст рекламы со ссылкой
    #конец                                                                  ← всё ниже — для подписчиков, не попадёт
    """
    if not is_footer_post(text):
        return None
    today = today or datetime.now(timezone.utc).date()
    pos, spans = 0, []
    for ln in text.split("\n"):
        spans.append((pos, pos + u16(ln), ln))
        pos += u16(ln) + 1
    blocks = {"base": [], "ad": []}
    mode, seen_tag, ad_until, ad_on = "base", False, None, False
    for a, b, ln in spans:
        low = ln.strip().lower()
        if not seen_tag:
            seen_tag = low == FOOTER_TAG
            continue
        if low.startswith("#конец") or low.startswith("#end"):
            break
        if low.startswith("#реклама") or low.startswith("#ad"):
            mode = "ad"
            ad_until = _until(low, posted)
            ad_on = ad_until is None or today <= ad_until
            continue
        if mode == "base" or ad_on:
            blocks[mode].append((a, b, ln))

    def trim(lines):
        while lines and not lines[0][2].strip():
            lines.pop(0)
        while lines and not lines[-1][2].strip():
            lines.pop()
        return lines

    base, ad = trim(blocks["base"]), trim(blocks["ad"])
    if not base:
        base = [(None, None, DEFAULT_FOOTER)]
    base_text = "\n".join(ln for _, _, ln in base)
    out, ents = "", []
    for a, b, ln in base + ad:
        if out:
            out += "\n"
        start = u16(out)
        if a is not None:
            for e in entities or ():
                x, y = max(a, e.offset), min(b, e.offset + e.length)
                if x < y:
                    d = ent_to_dict(e)
                    if d:
                        d.update(o=start + x - a, l=y - x)
                        ents.append(d)
        out += ln
    if u16(out) > MAX_FOOTER:
        log.warning("приписка в канале длиннее %d символов — беру стандартную", MAX_FOOTER)
        return {"text": DEFAULT_FOOTER, "ents": [], "ad_until": None, "base": DEFAULT_FOOTER, "too_long": True}
    return {"text": out, "ents": ents, "ad_until": ad_until.isoformat() if (ad and ad_until) else None,
            "base": base_text}


HELP = f"""⏱ Метроном {VERSION} — авторассылка. Команды (пиши их здесь, в Избранном):

Реклама
/text — ответом на своё сообщение: оно станет рекламой (форматирование, эмодзи, ссылки, фото сохранятся)
/text Текст рекламы — просто текст
/show — показать рекламу · /test — прислать её сюда

Чаты
/groups [слово] — твои группы с номерами
/add 1 4 7 — добавить по номерам из /groups
/add @chat или t.me/chat — по ссылке (t.me/chat/5 — в тему 5 форума)
/chats — список рассылки · /del 2 · /del all

Расписание
/every 30m · /every 2h · /every 1h30m — интервал (минимум {MIN_INTERVAL // 60} мин)
/on — включить · /off — выключить · /now — разослать сейчас
/on 3 · /off 3 — включить/выключить отдельный чат
/status — что происходит · /footer — приписка

Чаты, где писать нельзя (бан, только для админов, платные сообщения), отключаются сами — увидишь в /chats.
К сообщениям добавляется короткая приписка Метронома — так он остаётся бесплатным.
Новости и обновления: @{CHANNEL}"""


# ───────────────────────── сам бот ─────────────────────────

class Autopost:
    def __init__(self, client, data_path=None, gap=GAP, clock=time.time, sleep=asyncio.sleep, premium=False):
        self.client = client
        self.data_path = data_path or os.path.join(HERE, "data.json")
        self.gap = gap
        self.now = clock
        self.sleep = sleep
        self.st = {"enabled": False, "interval": 3600, "ad": None, "chats": [], "footer": None}
        self.caption_limit = TEXT_LIMIT if premium else CAPTION_LIMIT
        self.footer_checked = 0.0       # когда последний раз читали канал
        self.footer_ok = None           # удалось ли прочитать канал
        self.pause_until = 0.0
        self.last_groups = []
        self.started = self.now()
        self.total_sent = 0
        self._backup_sent = None
        self._backup_msg_id = None
        self._busy = asyncio.Lock()

    # ── хранение ──
    def _settings(self):
        return {"enabled": self.st["enabled"], "interval": self.st["interval"], "ad": self.st["ad"],
                "chats": [{k: c.get(k) for k in ("id", "title", "username", "topic", "on", "err")}
                          for c in self.st["chats"]]}

    def load_file(self):
        try:
            with open(self.data_path, encoding="utf-8") as f:
                self._apply(json.load(f))
            return True
        except (FileNotFoundError, ValueError):
            return False

    def _apply(self, d):
        self.st["enabled"] = bool(d.get("enabled", False))
        self.st["interval"] = max(MIN_INTERVAL, int(d.get("interval", 3600)))
        self.st["ad"] = d.get("ad")
        if d.get("footer"):
            self.st["footer"] = d["footer"]
        self.st["chats"] = []
        for c in d.get("chats", []):
            if "id" not in c:
                continue
            self.st["chats"].append({"id": int(c["id"]), "title": c.get("title") or str(c["id"]),
                                     "username": c.get("username"), "topic": c.get("topic"),
                                     "on": c.get("on", True), "err": c.get("err") or "",
                                     "next": float(c.get("next", 0)), "sent": int(c.get("sent", 0)), "fails": 0})

    def save(self):
        tmp = self.data_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.data_path)

    async def backup(self):
        """Копия настроек файлом в Избранное — переживёт переезд/сброс хостинга."""
        payload = json.dumps(self._settings(), ensure_ascii=False, indent=1, sort_keys=True)
        if payload == self._backup_sent:
            return
        try:
            f = io.BytesIO(payload.encode("utf-8"))
            f.name = "metronom_backup.json"
            msg = await self.client.send_file("me", f, caption=f"{BACKUP_TAG} — настройки Метронома, не удаляй",
                                              force_document=True, silent=True)
            old = self._backup_msg_id
            self._backup_msg_id = getattr(msg, "id", None)
            self._backup_sent = payload
            if old:
                await self.client.delete_messages("me", [old])
        except Exception as e:  # не критично
            log.warning("бэкап не сохранился: %s", e)

    async def restore(self):
        try:
            for tag in (BACKUP_TAG,) + OLD_BACKUP_TAGS:
                async for m in self.client.iter_messages("me", search=tag, limit=20):
                    if getattr(m, "file", None) and (m.message or "").startswith(tag):
                        raw = await self.client.download_media(m, file=bytes)
                        self._apply(json.loads(raw.decode("utf-8")))
                        self._backup_msg_id = m.id
                        self._backup_sent = json.dumps(self._settings(), ensure_ascii=False, indent=1, sort_keys=True)
                        return True
        except Exception as e:
            log.warning("не смог восстановить бэкап: %s", e)
        return False

    async def changed(self):
        self.save()
        await self.backup()

    async def say(self, text):
        for i in range(0, len(text), 3900):
            await self.client.send_message("me", text[i:i + 3900], link_preview=False, parse_mode=None)

    # ── реклама ──
    async def get_ad(self):
        ad = self.st["ad"]
        if not ad:
            return None
        if ad.get("text"):
            return ad["text"]
        msg = await self.client.get_messages("me", ids=ad["msg_id"])
        return msg or None

    # ── приписка из канала ──
    def footer(self):
        f = self.st.get("footer")
        if not f or not f.get("text"):
            return {"text": DEFAULT_FOOTER, "ents": []}
        if f.get("ad_until") and datetime.now(timezone.utc).date().isoformat() > f["ad_until"]:
            # реклама истекла, а канал давно не читался — показываем без неё
            base = f.get("base") or DEFAULT_FOOTER
            return {"text": base, "ents": [e for e in f["ents"] if e["o"] + e["l"] <= u16(base)]}
        return f

    async def refresh_footer(self):
        """Прочитать пост-«админку» в @metronom_tg. Не получилось — остаётся прошлая приписка."""
        self.footer_checked = self.now()
        try:
            post = None
            async for m in self.client.iter_messages(CHANNEL, search=FOOTER_TAG, limit=10):
                if is_footer_post(m.message):
                    post = m
                    break
            if post is None:  # поиск иногда не видит свежие посты — просмотреть последние
                async for m in self.client.iter_messages(CHANNEL, limit=100):
                    if is_footer_post(m.message):
                        post = m
                        break
        except Exception as e:
            self.footer_ok = False
            log.warning("не смог прочитать @%s: %s", CHANNEL, e.__class__.__name__)
            return False
        self.footer_ok = True
        posted = post.date.date() if post is not None and getattr(post, "date", None) else None
        f = parse_footer(post.message, post.entities, posted=posted) if post is not None else None
        f = f or {"text": DEFAULT_FOOTER, "ents": [], "ad_until": None, "base": DEFAULT_FOOTER}
        if f != self.st.get("footer"):
            self.st["footer"] = f
            self.save()
            log.info("приписка обновлена: %s", f["text"].replace("\n", " | "))
        return True

    def compose(self, ad):
        """Реклама + приписка → (текст, разметка, медиа, превью ссылки)."""
        if isinstance(ad, str):
            text, ents, media, preview = ad, [], None, True
        else:
            text = ad.message or ""
            ents = list(ad.entities or [])
            web = isinstance(ad.media, types.MessageMediaWebPage)
            media = ad.media if ad.media and not web else None
            preview = web
        f = self.footer()
        limit = self.caption_limit if media is not None else TEXT_LIMIT
        sep = "\n\n" if text.strip() else ""
        if f["text"] not in text and u16(text + sep + f["text"]) <= limit:
            shift = u16(text + sep)
            text += sep + f["text"]
            ents += [ent_from_dict(d, shift) for d in f["ents"]]
        return text, autolink(text, ents), media, preview

    async def _send(self, peer, msg, topic=None):
        text, ents, media, preview = msg
        if media is not None:
            return await self.client.send_file(peer, media, caption=text, formatting_entities=ents,
                                               reply_to=topic, parse_mode=None)
        return await self.client.send_message(peer, text, formatting_entities=ents, reply_to=topic,
                                              link_preview=preview, parse_mode=None)

    # ── отправка ──
    def _disable(self, c, reason):
        c["on"] = False
        c["err"] = reason
        log.info("чат отключён: %s — %s", c["title"], reason)

    async def send_one(self, c, ad):
        now = self.now()
        try:
            try:
                await self._send(c["id"], ad, c.get("topic"))
            except ValueError:  # Telegram «не знает» чат после перезапуска — найти по @имени
                if not c.get("username"):
                    raise
                await self._send(await self.client.get_input_entity(c["username"]), ad, c.get("topic"))
        except errors.SlowModeWaitError as e:
            c["next"] = now + e.seconds + 5
            return "slow"
        except errors.FloodWaitError as e:
            self.pause_until = now + e.seconds + 5
            c["next"] = self.pause_until
            if e.seconds > 120:
                await self.say(f"⏱ ⏳ Telegram попросил подождать {fmt_dur(e.seconds)} (FloodWait). Продолжу сам.")
            return "flood"
        except errors.PeerFloodError:
            self.st["enabled"] = False
            await self.say("⏱ ⛔ Telegram ограничил аккаунт за спам (PeerFlood). Рассылка ВЫКЛЮЧЕНА.\n"
                           "Проверь @SpamBot, подожди (обычно от суток) и включи /on. Увеличь интервал и убери лишние чаты.")
            return "peerflood"
        except FATAL:
            raise
        except FORBIDDEN as e:
            self._disable(c, _reason(e))
            await self.say(f"⏱ ⛔ «{c['title']}» отключён: {c['err']}")
            return "forbidden"
        except errors.RPCError as e:
            txt = f"{e.__class__.__name__} {getattr(e, 'message', '')}"
            if "PAYMENT" in txt.upper() or "STARS" in txt.upper():
                self._disable(c, "сообщения в этом чате платные (звёзды)")
                await self.say(f"⏱ ⛔ «{c['title']}» отключён: {c['err']}")
                return "forbidden"
            return await self._fail(c, txt, now)
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            c["next"] = now + 60
            log.warning("сеть: %s", e)
            return "net"
        except ValueError:
            return await self._fail(c, "чат не найден — ты точно в нём состоишь?", now)
        except Exception as e:
            return await self._fail(c, f"{e.__class__.__name__}: {e}", now)
        c["sent"] = c.get("sent", 0) + 1
        c["fails"] = 0
        c["err"] = ""
        c["next"] = now + self.st["interval"]
        self.total_sent += 1
        log.info("→ %s", c["title"])
        return "ok"

    async def _fail(self, c, txt, now):
        c["fails"] = c.get("fails", 0) + 1
        c["next"] = now + min(self.st["interval"], 15 * 60)
        log.warning("ошибка в «%s»: %s", c["title"], txt)
        if c["fails"] >= MAX_FAILS:
            self._disable(c, f"{MAX_FAILS} ошибки подряд: {txt[:120]}")
            await self.say(f"⏱ ⛔ «{c['title']}» отключён: {c['err']}")
        return "fail"

    async def run_due(self, force=False):
        """Одна проверка очереди: отправить во все чаты, которым пора."""
        if not (self.st["enabled"] or force) or self.now() < self.pause_until:
            return 0
        async with self._busy:
            ad = await self.get_ad()
            if ad is None:
                if self.st["enabled"]:
                    self.st["enabled"] = False
                    await self.say("⏱ Реклама не задана или удалена — рассылка выключена. /text, потом /on")
                    await self.changed()
                return 0
            ad = self.compose(ad)
            n = 0
            due = [c for c in self.st["chats"] if c["on"] and (force or c.get("next", 0) <= self.now())]
            if not due:
                return 0
            for i, c in enumerate(due):
                if not (self.st["enabled"] or force) or self.now() < self.pause_until:
                    break
                r = await self.send_one(c, ad)
                n += r == "ok"
                if r == "peerflood":
                    break
                if i < len(due) - 1:
                    await self.sleep(random.uniform(*self.gap))
            await self.changed()
            return n

    async def scheduler(self):
        while True:
            try:
                if self.now() - self.footer_checked >= FOOTER_EVERY:
                    await self.refresh_footer()
                await self.run_due()
            except FATAL:
                raise
            except Exception as e:
                log.exception("планировщик: %s", e)
            await self.sleep(TICK)

    # ── команды ──
    async def handle(self, text, reply_to_msg_id=None):
        m = re.match(r"(?s)^/(\w+)(?:@\w+)?\s*(.*)$", text.strip())
        if not m:
            return
        cmd, arg = m.group(1).lower(), m.group(2).strip()
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            return  # чужие команды не трогаем
        await fn(arg, reply_to_msg_id)

    async def cmd_help(self, arg, rid):
        await self.say(HELP)

    cmd_start = cmd_help

    async def cmd_text(self, arg, rid):
        if rid:
            self.st["ad"] = {"msg_id": rid}
        elif arg:
            self.st["ad"] = {"text": arg}
        else:
            return await self.say("⏱ Ответь командой /text на сообщение с рекламой или напиши /text Текст")
        await self.changed()
        await self.say("⏱ ✅ Реклама сохранена. Посмотреть: /test")

    async def cmd_show(self, arg, rid):
        ad = self.st["ad"]
        if not ad:
            return await self.say("⏱ Реклама не задана: /text")
        if ad.get("text"):
            return await self.say("⏱ Реклама (текст):\n\n" + ad["text"])
        msg = await self.get_ad()
        if not msg:
            return await self.say("⏱ Сообщение с рекламой удалено — задай заново: /text")
        await self.client.send_message("me", "⏱ ↑ вот это сообщение рассылается (+ приписка Метронома, как в /test)", reply_to=msg.id)

    async def cmd_test(self, arg, rid):
        ad = await self.get_ad()
        if ad is None:
            return await self.say("⏱ Реклама не задана: /text")
        msg = self.compose(ad)
        await self._send("me", msg)
        if self.footer()["text"] not in msg[0]:
            lim = self.caption_limit if msg[2] is not None else TEXT_LIMIT
            await self.say(f"⏱ ⚠️ Текст слишком длинный — приписка не влезает в лимит Telegram ({lim} символов "
                           f"{'для подписи к фото' if msg[2] is not None else ''}). Сократи на пару строк.")

    async def cmd_footer(self, arg, rid):
        ok = await self.refresh_footer()
        f = self.footer()
        src = f"из @{CHANNEL}" if ok else f"сохранённая (канал @{CHANNEL} сейчас недоступен)"
        extra = f"\nРеклама в приписке до {f['ad_until']}" if f.get("ad_until") else ""
        await self.say(f"⏱ Приписка ({src}), добавляется к каждому сообщению:\n\n{f['text']}{extra}")

    async def _groups(self):
        out = []
        async for d in self.client.iter_dialogs():
            e = d.entity
            if d.is_group or (d.is_channel and (getattr(e, "creator", False) or getattr(e, "admin_rights", None))):
                out.append({"id": utils.get_peer_id(e), "title": d.name or "без названия",
                            "username": getattr(e, "username", None), "forum": bool(getattr(e, "forum", False))})
        return out

    async def cmd_groups(self, arg, rid):
        gs = await self._groups()
        if arg:
            gs = [g for g in gs if arg.lower() in g["title"].lower() or arg.lower() in (g["username"] or "").lower()]
        self.last_groups = gs
        if not gs:
            return await self.say("⏱ Групп не найдено. Сначала вступи в чаты в приложении Telegram.")
        added = {c["id"] for c in self.st["chats"]}
        lines = [f"{i}. {'✅ ' if g['id'] in added else ''}{g['title']}"
                 + (f" (@{g['username']})" if g["username"] else "") + (" [форум]" if g["forum"] else "")
                 for i, g in enumerate(gs, 1)]
        await self.say("⏱ Твои группы (✅ — уже в рассылке):\n" + "\n".join(lines)
                       + "\n\nДобавить: /add 1 3 5")

    async def cmd_add(self, arg, rid):
        if not arg:
            return await self.say("⏱ /add 1 3 5 (номера из /groups) или /add @chat")
        toks = re.split(r"[\s,]+", arg)
        added, bad = [], []
        for tok in toks:
            if not tok:
                continue
            if re.fullmatch(r"\d+(-\d+)?", tok) and not re.fullmatch(r"\d{6,}", tok):
                if not self.last_groups:
                    self.last_groups = await self._groups()
                a, _, b = tok.partition("-")
                rng = range(int(a), int(b or a) + 1)
                for k in rng:
                    if 1 <= k <= len(self.last_groups):
                        g = self.last_groups[k - 1]
                        added.append(self._add(g["id"], g["title"], g["username"], None))
                    else:
                        bad.append(f"{k} — нет такого номера")
                continue
            t = parse_target(tok)
            if not t:
                bad.append(f"{tok} — не понял (нужно @chat, t.me/chat или номер)")
                continue
            ref, topic = t
            try:
                e = await self.client.get_entity(ref)
            except Exception as ex:
                bad.append(f"{tok} — не нашёл ({ex.__class__.__name__}). Вступи в чат в приложении.")
                continue
            title = getattr(e, "title", None) or utils.get_display_name(e) or str(ref)
            added.append(self._add(utils.get_peer_id(e), title, getattr(e, "username", None), topic))
        added = [a for a in added if a]
        if added:
            await self.changed()
        msg = []
        if added:
            msg.append("✅ Добавлено: " + ", ".join(added))
        if bad:
            msg.append("⚠️ " + "\n⚠️ ".join(bad))
        if not added and not bad:
            msg.append("Эти чаты уже в рассылке.")
        if added and not self.st["enabled"]:
            msg.append("Рассылка выключена — включи /on" if self.st["ad"] else "Задай рекламу /text и включи /on")
        await self.say("⏱ " + "\n".join(msg))

    def _add(self, cid, title, username, topic):
        for c in self.st["chats"]:
            if c["id"] == cid and c.get("topic") == topic:
                return None
        self.st["chats"].append({"id": cid, "title": title + (f" #{topic}" if topic else ""), "username": username,
                                 "topic": topic, "on": True, "err": "", "next": 0.0, "sent": 0, "fails": 0})
        return title

    async def cmd_chats(self, arg, rid):
        if not self.st["chats"]:
            return await self.say("⏱ Рассылка пустая. /groups → /add")
        now = self.now()
        lines = []
        for i, c in enumerate(self.st["chats"], 1):
            if not c["on"]:
                st = "⛔ " + (c["err"] or "выключен")
            elif not self.st["enabled"]:
                st = "⏸"
            else:
                st = "⏱ сейчас" if c.get("next", 0) <= now else f"⏱ через {fmt_dur(c['next'] - now)}"
            lines.append(f"{i}. {c['title']} — {st}, отправлено {c.get('sent', 0)}")
        await self.say("⏱ Рассылка:\n" + "\n".join(lines) + "\n\nУбрать: /del 2 · выключить/включить чат: /off 2 · /on 2")

    def _pick(self, arg):
        n = len(self.st["chats"])
        idx = set()
        for tok in re.split(r"[\s,]+", arg):
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok or "")
            if m:
                for k in range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1):
                    if 1 <= k <= n:
                        idx.add(k - 1)
        return sorted(idx)

    async def cmd_del(self, arg, rid):
        if arg.lower() in ("all", "все", "всё"):
            n = len(self.st["chats"])
            self.st["chats"] = []
            await self.changed()
            return await self.say(f"⏱ Убрал все чаты ({n}).")
        idx = self._pick(arg)
        if not idx:
            return await self.say("⏱ /del 2 (номер из /chats) или /del all")
        gone = [self.st["chats"][i]["title"] for i in idx]
        self.st["chats"] = [c for i, c in enumerate(self.st["chats"]) if i not in idx]
        await self.changed()
        await self.say("⏱ Убрал: " + ", ".join(gone))

    async def cmd_every(self, arg, rid):
        sec = parse_interval(arg)
        if not sec:
            return await self.say("⏱ Примеры: /every 30m · /every 2h · /every 1h30m · /every 45")
        if sec < MIN_INTERVAL:
            await self.say(f"⏱ Минимум {MIN_INTERVAL // 60} мин — чаще Telegram быстро ограничит аккаунт. Ставлю {MIN_INTERVAL // 60} мин.")
            sec = MIN_INTERVAL
        old = self.st["interval"]
        self.st["interval"] = sec
        for c in self.st["chats"]:  # пересчитать очередь под новый интервал
            if c.get("next", 0) > self.now():
                c["next"] = c["next"] - old + sec
        await self.changed()
        await self.say(f"⏱ ✅ Интервал: каждые {fmt_dur(sec)} в каждый чат.")

    async def cmd_on(self, arg, rid):
        if arg:
            idx = self._pick(arg)
            for i in idx:
                c = self.st["chats"][i]
                c.update(on=True, err="", fails=0, next=0.0)
            await self.changed()
            return await self.say("⏱ Включил: " + ", ".join(self.st["chats"][i]["title"] for i in idx) if idx
                                  else "⏱ /on 2 — номер из /chats")
        if not self.st["ad"]:
            return await self.say("⏱ Сначала реклама: /text")
        if not any(c["on"] for c in self.st["chats"]):
            return await self.say("⏱ Нет включённых чатов: /groups → /add")
        self.st["enabled"] = True
        await self.changed()
        await self.say(f"⏱ ▶️ Рассылка включена: каждые {fmt_dur(self.st['interval'])}, "
                       f"чатов {sum(c['on'] for c in self.st['chats'])}. Первые сообщения уйдут в течение минуты.")

    async def cmd_off(self, arg, rid):
        if arg:
            idx = self._pick(arg)
            for i in idx:
                self.st["chats"][i].update(on=False, err="выключен вручную")
            await self.changed()
            return await self.say("⏱ Выключил: " + ", ".join(self.st["chats"][i]["title"] for i in idx) if idx
                                  else "⏱ /off 2 — номер из /chats")
        self.st["enabled"] = False
        await self.changed()
        await self.say("⏱ ⏸ Рассылка выключена. Включить: /on")

    async def cmd_now(self, arg, rid):
        if not self.st["ad"]:
            return await self.say("⏱ Сначала реклама: /text")
        await self.say("⏱ Рассылаю во все включённые чаты…")
        n = await self.run_due(force=True)
        await self.say(f"⏱ Готово: отправлено {n}. Подробно: /chats")

    async def cmd_status(self, arg, rid):
        on = sum(c["on"] for c in self.st["chats"])
        now = self.now()
        nxt = [c["next"] for c in self.st["chats"] if c["on"]]
        lines = [f"⏱ Метроном {VERSION}",
                 f"Состояние: {'▶️ работает' if self.st['enabled'] else '⏸ выключена'}",
                 f"Интервал: каждые {fmt_dur(self.st['interval'])}",
                 f"Реклама: {'есть' if self.st['ad'] else 'не задана (/text)'}",
                 f"Чатов: {on} включено из {len(self.st['chats'])}",
                 f"Отправлено с запуска: {self.total_sent}, работает {fmt_dur(now - self.started)}"]
        if self.st["enabled"] and nxt:
            lines.append("Следующая отправка: " + ("сейчас" if min(nxt) <= now else f"через {fmt_dur(min(nxt) - now)}"))
        if now < self.pause_until:
            lines.append(f"⏳ Пауза от Telegram ещё {fmt_dur(self.pause_until - now)}")
        await self.say("\n".join(lines))


def _reason(e):
    n = e.__class__.__name__
    return {
        "ChatWriteForbiddenError": "писать запрещено (только админы или ты в муте)",
        "UserBannedInChannelError": "ты забанен в этом чате",
        "ChannelPrivateError": "чат закрыт или тебя выгнали",
        "ChatAdminRequiredError": "нужны права админа",
        "ChatRestrictedError": "чат ограничен",
        "ChatSendPlainForbiddenError": "в чате запрещён обычный текст",
        "ChatSendMediaForbiddenError": "в чате запрещены фото/медиа",
        "ChatGuestSendForbiddenError": "сначала надо вступить в чат",
        "UserNotParticipantError": "ты не участник чата",
    }.get(n, n)


# ───────────────────────── запуск ─────────────────────────

async def health_server():
    """Некоторым хостингам нужен открытый порт — отвечаем «ok»."""
    port = os.environ.get("PORT") or os.environ.get("SERVER_PORT")
    if not port:
        return
    async def h(reader, writer):
        try:
            await asyncio.wait_for(reader.read(1024), 5)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    try:
        await asyncio.start_server(h, "0.0.0.0", int(port))
        log.info("порт %s открыт (проверка хостинга)", port)
    except Exception as e:
        log.warning("порт %s не открылся: %s", port, e)


async def amain():
    cfg = get_config()
    miss = [k for k in ("API_ID", "API_HASH", "SESSION") if not cfg.get(k)]
    if miss:
        log.error("Не хватает настроек: %s. Запусти login.bat на своём ПК — он создаст config.env, "
                  "и положи config.env рядом с main.py.", ", ".join(miss))
        await health_server()
        await asyncio.sleep(3600)
        return 1
    try:
        session = StringSession(cfg["SESSION"].strip())
        api_id = int(cfg["API_ID"])
    except ValueError:
        log.error("SESSION или API_ID испорчены — скопируй их из config.env заново целиком (SESSION — одна длинная строка).")
        return 2
    client = TelegramClient(session, api_id, cfg["API_HASH"],
                            device_model="Metronom", system_version=sys.platform, app_version=VERSION,
                            flood_sleep_threshold=60)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("Сессия недействительна (вышли из аккаунта или Telegram её сбросил). Запусти login.bat заново.")
        return 2
    me = await client.get_me()
    log.info("вошёл как %s (id %s)", utils.get_display_name(me), me.id)
    await client.get_dialogs()  # подгрузить чаты, иначе Telegram «не знает» их после перезапуска
    bot = Autopost(client, premium=bool(getattr(me, "premium", False)))
    src = "файл" if bot.load_file() else ("бэкап из Избранного" if await bot.restore() else "новые")
    log.info("настройки: %s; чатов %d; рассылка %s", src, len(bot.st["chats"]), "вкл" if bot.st["enabled"] else "выкл")

    @client.on(events.NewMessage(chats="me", outgoing=True, pattern=r"^/"))
    async def on_cmd(ev):
        try:
            await bot.handle(ev.raw_text, ev.message.reply_to_msg_id)
        except Exception as e:
            log.exception("команда")
            await bot.say(f"⏱ Ошибка: {e.__class__.__name__}: {e}")

    await health_server()
    await bot.refresh_footer()
    await bot.say(f"⏱ Метроном {VERSION} запущен ({'рассылка работает' if bot.st['enabled'] else 'рассылка выключена'}). "
                  f"Команды: /help · новости: @{CHANNEL}")
    task = asyncio.create_task(bot.scheduler())
    try:
        done, _ = await asyncio.wait([task, asyncio.ensure_future(client.run_until_disconnected())],
                                     return_when=asyncio.FIRST_COMPLETED)
        for f in done:
            if f.exception():
                raise f.exception()
    except FATAL as e:
        log.error("Telegram отключил сессию: %s. Если запущено в двух местах — оставь одно. "
                  "Иначе запусти login.bat заново.", e.__class__.__name__)
        return 3
    finally:
        await client.disconnect()
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%d.%m %H:%M:%S",
                        stream=sys.stdout)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass
    sys.exit(asyncio.run(amain()) or 0)


if __name__ == "__main__":
    main()
