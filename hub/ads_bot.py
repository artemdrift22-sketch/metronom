"""Метроном · магазин рекламы — Telegram-бот (Bot API), который продаёт строку в приписке за звёзды.

Рекламодатель пишет боту → присылает текст → выбирает дни → ты одобряешь кнопкой → он платит ⭐ →
бот сам ставит рекламу в служебный пост #metronom в канале @metronom_tg и снимает, когда срок вышел.
Все Метрономы перечитывают этот пост раз в 30 минут и добавляют рекламу к каждому сообщению.

Настройки (config.env рядом или переменные): ADS_BOT_TOKEN, ADMIN_ID, PRICE (⭐ за сутки, по умолчанию 50).
Без зависимостей — только стандартный Python 3.10+.
"""
import asyncio
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
CHANNEL = "metronom_tg"
BASE_FOOTER = "⏱ Отправлено через Метроном — бесплатная авторассылка: @metronom_tg"
AD_PREFIX = "📢 Реклама: "
AD_MAX = 150                    # символов в тексте рекламы
AD_LINES = 2
DAYS = (1, 3, 7, 14, 30)
SYNC_EVERY = 60                 # как часто сверять служебный пост, сек
REPOST_AFTER = 40               # столько новых постов после служебного — перевыложить его ниже
BACKUP_TAG = "#ads_backup"
OK_TYPES = {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "text_link", "url", "mention"}

log = logging.getLogger("ads")


def u16(s):
    return len(s.encode("utf-16-le")) // 2


def utc_today():
    return datetime.now(timezone.utc).date()


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


def get_config():
    cfg = load_env(os.path.join(HERE, "config.env"))
    for k in ("ADS_BOT_TOKEN", "ADMIN_ID", "PRICE"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def dn(n):
    return f"{n} " + ("день" if n % 10 == 1 and n % 100 != 11 else
                      "дня" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "дней")


def d(x):
    return x.strftime("%d.%m")


def oid_of(payload):
    m = re.fullmatch(r"order:(\d+)", payload or "")
    return int(m.group(1)) if m else 0


def kb(*rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": c} for t, c in row] for row in rows]}


class ApiError(Exception):
    def __init__(self, desc, code=None, params=None):
        super().__init__(desc)
        self.desc, self.code, self.params = desc or "", code, params or {}


# ───────────────────────── Bot API без библиотек ─────────────────────────

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
            if res.get("error_code") == 429 and wait and attempt < 2:
                await asyncio.sleep(wait + 1)
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


# ───────────────────────── магазин ─────────────────────────

class Shop:
    def __init__(self, api, admin_id=None, price=50, data_path=None, today=utc_today, channel=CHANNEL):
        self.api = api
        self.admin = int(admin_id) if admin_id else None
        self.data_path = data_path or os.path.join(HERE, "ads.json")
        self.today = today
        self.chat = "@" + channel
        self.username = "metronom_ads_bot"
        self.base_footer = BASE_FOOTER
        self.st = {"price": int(price), "open": True, "orders": [], "next_id": 1, "post_id": None,
                   "post_text": None, "last_post_seen": 0, "earned": 0, "backup_id": None}
        self.users = {}           # uid → шаг диалога (в памяти)
        self.dirty = False
        self._lock = asyncio.Lock()

    # ── хранение ──
    def load(self):
        try:
            with open(self.data_path, encoding="utf-8") as f:
                self.st.update(json.load(f))
            return True
        except (FileNotFoundError, ValueError):
            return False

    def save(self):
        tmp = self.data_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.data_path)
        self.dirty = True

    async def backup(self):
        """Копия заказов файлом в чат с админом (закреплена) — переживёт сброс хостинга."""
        if not (self.dirty and self.admin):
            return
        self.dirty = False
        try:
            raw = json.dumps(self.st, ensure_ascii=False, indent=1).encode()
            m = await self.api("sendDocument", chat_id=self.admin, disable_notification=True,
                               caption=f"{BACKUP_TAG} — заказы рекламы, не удаляй и не открепляй",
                               _document=("ads_state.json", raw))
            old, self.st["backup_id"] = self.st.get("backup_id"), m["message_id"]
            await self.api("pinChatMessage", chat_id=self.admin, message_id=m["message_id"], disable_notification=True)
            if old:
                await self._quiet("deleteMessage", chat_id=self.admin, message_id=old)
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump(self.st, f, ensure_ascii=False, indent=1)
        except Exception as e:
            self.dirty = True
            log.warning("бэкап не сохранился: %s", e)

    async def restore(self):
        if not self.admin:
            return False
        try:
            chat = await self.api("getChat", chat_id=self.admin)
            pm = chat.get("pinned_message") or {}
            if pm.get("document") and (pm.get("caption") or "").startswith(BACKUP_TAG):
                self.st.update(json.loads(await self.api.download(pm["document"]["file_id"])))
                self.st["backup_id"] = pm["message_id"]
                self.save()
                self.dirty = False
                return True
        except Exception as e:
            log.warning("не смог восстановить бэкап: %s", e)
        return False

    async def _quiet(self, method, **p):
        try:
            return await self.api(method, **p)
        except Exception as e:
            log.info("%s: %s", method, e)
            return None

    async def send(self, chat_id, text, **p):
        return await self._quiet("sendMessage", chat_id=chat_id, text=text,
                                 link_preview_options={"is_disabled": True}, **p)

    async def to_admin(self, text, **p):
        if self.admin:
            return await self.send(self.admin, text, **p)

    # ── расписание ──
    def order(self, oid):
        return next((o for o in self.st["orders"] if o["id"] == oid), None)

    def booked(self):
        return sorted((o for o in self.st["orders"] if o["status"] == "paid" and o.get("start")),
                      key=lambda o: o["start"])

    def free_from(self):
        t = self.today()
        ends = [date.fromisoformat(o["end"]) for o in self.booked() if date.fromisoformat(o["end"]) >= t]
        return max(ends) + timedelta(days=1) if ends else t

    def schedule(self, o):
        start = self.free_from()
        # стартуем сегодня — остаток сегодняшнего дня в подарок, дальше N полных суток
        end = start + timedelta(days=o["days"] - (0 if start == self.today() else 1))
        o.update(start=start.isoformat(), end=end.isoformat())

    def live(self):
        t = self.today().isoformat()
        return next((o for o in self.booked() if o["start"] <= t <= o["end"]), None)

    # ── служебный пост в канале ──
    def desired_post(self):
        ad = self.live()
        lines = ["#metronom", self.base_footer]
        ents = []
        if ad:
            lines.append(f"#реклама до {date.fromisoformat(ad['end']).strftime('%d.%m.%Y')}")
            head = "\n".join(lines) + "\n" + AD_PREFIX
            shift = u16(head)
            ents = [dict(e, offset=e["offset"] + shift) for e in ad.get("entities", [])]
            lines.append(AD_PREFIX + ad["text"])
        lines += ["#конец", "Служебный пост: отсюда все Метрономы берут приписку к сообщениям.",
                  f"Реклама в приписке — @{self.username}"]
        return "\n".join(lines), ents

    async def sync(self, force=False):
        text, ents = self.desired_post()
        pid = self.st.get("post_id")
        repost = force or not pid or self.st.get("last_post_seen", 0) - pid >= REPOST_AFTER
        if not repost and text == self.st.get("post_text"):
            return False
        if not repost:
            try:
                await self.api("editMessageText", chat_id=self.chat, message_id=pid, text=text, entities=ents,
                               link_preview_options={"is_disabled": True})
                self.st["post_text"] = text
                self.save()
                return True
            except ApiError as e:
                if "not modified" in e.desc:
                    self.st["post_text"] = text
                    self.save()
                    return False
                log.warning("не смог изменить служебный пост (%s) — выложу новый", e.desc)
        try:
            m = await self.api("sendMessage", chat_id=self.chat, text=text, entities=ents, disable_notification=True,
                               link_preview_options={"is_disabled": True})
        except ApiError as e:
            log.error("не могу писать в %s: %s. Добавь бота админом канала с правом публиковать и "
                      "редактировать сообщения.", self.chat, e.desc)
            return False
        self.st.update(post_id=m["message_id"], post_text=text,
                       last_post_seen=max(self.st.get("last_post_seen", 0), m["message_id"]))
        self.save()
        if pid:
            await self._quiet("deleteMessage", chat_id=self.chat, message_id=pid)
        return True

    async def tick(self):
        """Раз в минуту: начать/закончить показы, обновить пост, бэкап."""
        async with self._lock:
            t = self.today().isoformat()
            for o in self.booked():
                if o["start"] <= t <= o["end"] and not o.get("told_live"):
                    o["told_live"] = True
                    self.save()
                    await self.send(o["user_id"], f"✅ Реклама №{o['id']} в эфире до {d(date.fromisoformat(o['end']))} "
                                                  "включительно. В течение 30 минут она появится у всех Метрономов.")
                    if o["user_id"] != self.admin:
                        await self.to_admin(f"▶️ Реклама №{o['id']} в эфире.")
                if o["end"] < t and not o.get("told_done"):
                    o["told_done"] = True
                    self.save()
                    await self.send(o["user_id"], f"Реклама №{o['id']} закончилась. Спасибо! Ещё раз — /buy")
            await self.sync()
            await self.backup()

    # ── входящие ──
    async def handle(self, u):
        async with self._lock:
            if "channel_post" in u:
                ch = u["channel_post"].get("chat", {})
                if (ch.get("username") or "").lower() == self.chat[1:].lower():
                    self.st["last_post_seen"] = max(self.st.get("last_post_seen", 0), u["channel_post"]["message_id"])
            elif "pre_checkout_query" in u:
                await self.on_precheckout(u["pre_checkout_query"])
            elif "callback_query" in u:
                await self.on_button(u["callback_query"])
            elif "message" in u and u["message"].get("chat", {}).get("type") == "private":
                await self.on_message(u["message"])

    async def on_message(self, m):
        uid = m["from"]["id"]
        if "successful_payment" in m:
            return await self.on_paid(m)
        text = m.get("text") or ""
        cmd = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", text, re.S)
        if cmd:
            name, arg = cmd.group(1).lower(), cmd.group(2).strip()
            if uid == self.admin and hasattr(self, "adm_" + name):
                return await getattr(self, "adm_" + name)(arg)
            fn = getattr(self, "cmd_" + name, None)
            if fn:
                if name in ("start", "cancel", "buy"):
                    self.users.pop(uid, None)
                return await fn(uid, arg, m)
            return await self.send(uid, "Не знаю такую команду. /start")
        step = self.users.get(uid, {})
        if step.get("step") == "text":
            return await self.got_text(uid, m)
        if step.get("step") == "support":
            self.users.pop(uid, None)
            name = m["from"].get("username") and "@" + m["from"]["username"] or m["from"].get("first_name", "")
            await self.to_admin(f"🆘 Вопрос по оплате от {name} (id {uid}):\n\n{text}\n\nОтветить: /reply {uid} текст")
            return await self.send(uid, "Передал. Ответ придёт сюда.")
        if step.get("step") == "admin_text" and uid == self.admin:
            return await self.got_text(uid, m, manual_days=step["days"])
        await self.cmd_start(uid, "", m)

    async def cmd_start(self, uid, arg, m):
        subs = await self._quiet("getChatMemberCount", chat_id=self.chat)
        live = self.live()
        start = self.free_from()
        lines = [
            "⏱ Реклама в Метрономе",
            "",
            "Метроном — бесплатная авторассылка в Telegram-чаты. Под каждым сообщением, которое отправляют "
            "его пользователи, стоит приписка — и в ней может быть твоя строка:",
            "",
            "…сообщение пользователя…",
            self.base_footer,
            AD_PREFIX + "твой текст со ссылкой",
            "",
            f"Цена: {self.st['price']} ⭐ за сутки. Одна реклама за раз — никто не делит с тобой место.",
            "Ближайший старт: " + ("сегодня, сразу после оплаты" if start == self.today() else d(start)),
            f"Канал проекта: @{CHANNEL}" + (f" — {subs} подписчиков" if subs else ""),
            "Точный охват не считается: Метроном не собирает статистику пользователей.",
        ]
        if live and live["user_id"] == uid:
            lines.append(f"\nТвоя реклама сейчас в эфире до {d(date.fromisoformat(live['end']))}.")
        if not self.admin:
            lines.append(f"\n⚙️ ADMIN_ID не задан. Если ты владелец — впиши ADMIN_ID={uid} и перезапусти бота.")
        await self.send(uid, "\n".join(lines),
                        reply_markup=kb([("🛒 Купить рекламу", "buy")], [("📋 Мои заказы", "my")]))

    cmd_help = cmd_start

    async def cmd_buy(self, uid, arg, m):
        if not self.st["open"]:
            return await self.send(uid, "Продажа рекламы сейчас на паузе. Загляни позже.")
        if any(o["user_id"] == uid and o["status"] in ("review", "approved") for o in self.st["orders"]):
            return await self.send(uid, "У тебя уже есть заявка в работе — /my. Новую можно после неё.")
        self.users[uid] = {"step": "text"}
        await self.send(uid, f"Пришли текст рекламы одним сообщением:\n"
                             f"• до {AD_MAX} символов, 1–{AD_LINES} строки, без фото\n"
                             f"• можно @username, ссылку или спрятать ссылку в слово (выдели слово → «Добавить ссылку»)\n"
                             f"• бот сам поставит перед текстом «{AD_PREFIX.strip()}»\n\n/cancel — отмена")

    async def cmd_cancel(self, uid, arg, m):
        await self.send(uid, "Ок, отменил. /start")

    async def cmd_my(self, uid, arg, m):
        mine = [o for o in self.st["orders"] if o["user_id"] == uid]
        if not mine:
            return await self.send(uid, "Заказов пока нет. /buy")
        await self.send(uid, "Твои заказы:\n" + "\n".join(self._line(o) for o in mine[-10:]))

    async def cmd_paysupport(self, uid, arg, m):
        self.users[uid] = {"step": "support"}
        await self.send(uid, "Опиши проблему с оплатой одним сообщением (номер заказа, что случилось) — передам владельцу. "
                             "Если реклама не вышла по нашей вине, звёзды вернём.")

    cmd_support = cmd_paysupport

    async def cmd_terms(self, uid, arg, m):
        await self.send(uid, "Условия: реклама — одна строка в приписке Метронома на оплаченное число суток (по UTC). "
                             "Перед оплатой заявку проверяет владелец; запрещено: мошенничество, казино, наркотики, "
                             "18+, взлом, всё незаконное. Показы зависят от числа активных пользователей — "
                             "точный охват не гарантируется. Если реклама не вышла — возврат звёзд через /paysupport.")

    def _line(self, o):
        st = {"review": "⏳ на проверке", "approved": "💳 ждёт оплаты", "rejected": "❌ отклонена",
              "cancelled": "🚫 снята", "refunded": "↩️ возврат"}.get(o["status"])
        if o["status"] == "paid":
            t = self.today().isoformat()
            st = ("▶️ в эфире" if o["start"] <= t <= o["end"] else "✔️ показана" if o["end"] < t else "🗓 в очереди")
            st += f" {d(date.fromisoformat(o['start']))}–{d(date.fromisoformat(o['end']))}"
        who = "" if o["user_id"] == self.admin or not o.get("user_name") else f" · {o['user_name']}"
        return f"№{o['id']} · {dn(o['days'])} · {o['price']} ⭐ · {st}{who}\n   {o['text'][:70]}"

    async def got_text(self, uid, m, manual_days=None):
        if any(k in m for k in ("photo", "video", "document", "animation", "sticker", "voice", "audio")):
            return await self.send(uid, "Нужен только текст, без фото и файлов. Пришли ещё раз.")
        raw = m.get("text") or ""
        text = raw.strip()
        lead = u16(raw[:len(raw) - len(raw.lstrip())])
        if not text:
            return await self.send(uid, "Пустой текст. Пришли ещё раз.")
        if len(text) > AD_MAX:
            return await self.send(uid, f"Длинно: {len(text)} символов, можно до {AD_MAX}. Сократи и пришли ещё раз.")
        if text.count("\n") >= AD_LINES:
            return await self.send(uid, f"Можно максимум {AD_LINES} строки. Сократи и пришли ещё раз.")
        n = u16(text)
        ents = []
        for e in m.get("entities") or []:
            if e["type"] not in OK_TYPES:
                continue
            a, b = max(0, e["offset"] - lead), min(n, e["offset"] - lead + e["length"])
            if a < b:
                ents.append({k: v for k, v in dict(e, offset=a, length=b - a).items()
                             if k in ("type", "offset", "length", "url")})
        if manual_days:
            self.users.pop(uid, None)
            o = self._new(uid, "вручную", text, ents, manual_days, 0)
            self.schedule(o)
            o["status"] = "paid"
            self.save()
            await self.send(uid, f"✅ Реклама №{o['id']} поставлена: {d(date.fromisoformat(o['start']))}–"
                                 f"{d(date.fromisoformat(o['end']))}.")
            return await self.sync()
        self.users[uid] = {"step": "days", "text": text, "ents": ents}
        p = self.st["price"]
        rows = [[(f"{dn(k)} — {k * p} ⭐", f"days:{k}")] for k in DAYS]
        await self.send(uid, "Так будет выглядеть приписка:")
        await self.send(uid, self.base_footer + "\n" + AD_PREFIX + text,
                        entities=[dict(e, offset=e["offset"] + u16(self.base_footer + "\n" + AD_PREFIX)) for e in ents])
        await self.send(uid, "На сколько суток?", reply_markup=kb(*rows, [("✏️ Другой текст", "buy")]))

    def _new(self, uid, user_name, text, ents, days, price):
        o = {"id": self.st["next_id"], "user_id": uid, "user_name": user_name, "text": text, "entities": ents,
             "days": days, "price": price, "status": "review", "created": time.time()}
        self.st["next_id"] += 1
        self.st["orders"].append(o)
        return o

    async def on_button(self, q):
        uid, data = q["from"]["id"], q.get("data") or ""
        await self._quiet("answerCallbackQuery", callback_query_id=q["id"])
        msg = q.get("message") or {}
        if data == "buy":
            self.users.pop(uid, None)
            return await self.cmd_buy(uid, "", msg)
        if data == "my":
            return await self.cmd_my(uid, "", msg)
        if data.startswith("days:"):
            step = self.users.get(uid)
            if not step or step.get("step") != "days":
                return await self.send(uid, "Начни заново: /buy")
            step["days"] = int(data[5:])
            start = self.free_from()
            return await self.send(uid, f"Заявка: {dn(step['days'])}, {step['days'] * self.st['price']} ⭐.\n"
                                        f"Ближайший старт: {'сегодня' if start == self.today() else d(start)} "
                                        "(точная дата — в момент оплаты).\n"
                                        "Сначала заявку проверит владелец, потом придёт счёт на оплату звёздами.",
                                   reply_markup=kb([("✅ Отправить на проверку", "send")], [("✏️ Изменить", "buy")]))
        if data == "send":
            step = self.users.pop(uid, None)
            if not step or "days" not in step:
                return await self.send(uid, "Начни заново: /buy")
            fr = q["from"]
            name = "@" + fr["username"] if fr.get("username") else fr.get("first_name", "") + f" (id {uid})"
            o = self._new(uid, name, step["text"], step["ents"], step["days"], step["days"] * self.st["price"])
            self.save()
            await self.send(uid, f"⏳ Заявка №{o['id']} отправлена на проверку. Как только одобрят — пришлю счёт.")
            head = f"🆕 Заявка №{o['id']} от {name}: {dn(o['days'])}, {o['price']} ⭐\n\n" + AD_PREFIX
            await self.to_admin(head + o["text"], entities=[dict(e, offset=e["offset"] + u16(head)) for e in o["entities"]],
                                reply_markup=kb([("✅ Одобрить", f"ok:{o['id']}"), ("❌ Отклонить", f"no:{o['id']}")]))
            if not self.admin:
                log.warning("ADMIN_ID не задан — заявку №%s некому одобрить", o["id"])
            return
        if (data.startswith("ok:") or data.startswith("no:")) and uid == self.admin:
            o = self.order(int(data[3:]))
            if not o or o["status"] != "review":
                return await self.to_admin("Эта заявка уже обработана.")
            if data.startswith("ok:"):
                return await self.approve(o)
            return await self.reject(o, "")

    async def approve(self, o):
        o["status"] = "approved"
        self.save()
        start = self.free_from()
        await self.api("sendInvoice", chat_id=o["user_id"], title=f"Реклама в Метрономе — {dn(o['days'])}",
                       description=f"Строка в приписке Метронома на {dn(o['days'])}. "
                                   f"Старт: {'сегодня' if start == self.today() else d(start)}.",
                       payload=f"order:{o['id']}", currency="XTR", provider_token="",
                       prices=[{"label": f"Реклама {dn(o['days'])}", "amount": o["price"]}])
        await self.send(o["user_id"], f"✅ Заявка №{o['id']} одобрена — оплати счёт выше. Место занимается в момент оплаты.")
        await self.to_admin(f"✅ №{o['id']} одобрена, счёт отправлен.")

    async def reject(self, o, reason):
        o["status"] = "rejected"
        o["reason"] = reason
        self.save()
        await self.send(o["user_id"], f"❌ Заявка №{o['id']} отклонена" + (f": {reason}" if reason else ".")
                        + "\nМожно исправить текст и подать заново — /buy")
        await self.to_admin(f"❌ №{o['id']} отклонена.")

    async def on_precheckout(self, q):
        o = self.order(oid_of(q.get("invoice_payload")))
        ok = bool(o and o["status"] == "approved" and q.get("currency") == "XTR" and q.get("total_amount") == o["price"]
                  and self.st["open"])
        p = {"pre_checkout_query_id": q["id"], "ok": ok}
        if not ok:
            p["error_message"] = "Заявка неактуальна или продажа на паузе. Оформи заново: /buy"
        await self.api("answerPreCheckoutQuery", **p)

    async def on_paid(self, m):
        sp = m["successful_payment"]
        uid = m["from"]["id"]
        o = self.order(oid_of(sp.get("invoice_payload")))
        if not o:
            await self.to_admin(f"⚠️ Пришла оплата {sp.get('total_amount')} ⭐ от id {uid} без заказа — "
                                f"верни: /refund_id {uid} {sp.get('telegram_payment_charge_id')}")
            return
        self.schedule(o)
        o.update(status="paid", charge_id=sp.get("telegram_payment_charge_id"), paid=time.time())
        self.st["earned"] += int(sp.get("total_amount", 0))
        self.save()
        s, e = date.fromisoformat(o["start"]), date.fromisoformat(o["end"])
        await self.send(uid, f"💰 Оплачено, спасибо! Реклама №{o['id']}: {d(s)}–{d(e)} включительно"
                             + (" (сегодня — в подарок)." if s == self.today() else ".")
                             + " Напишу, когда начнётся и закончится. Вопросы по оплате: /paysupport")
        await self.to_admin(f"💰 +{sp.get('total_amount')} ⭐ — №{o['id']} ({o.get('user_name')}), {d(s)}–{d(e)}. "
                            f"Всего заработано: {self.st['earned']} ⭐")
        await self.sync()

    # ── команды владельца ──
    async def adm_admin(self, arg):
        await self.to_admin(
            "Команды владельца:\n"
            "/queue — заявки, очередь, эфир\n"
            "/price 50 — цена за сутки в ⭐ (сейчас " + str(self.st["price"]) + ")\n"
            "/add 3 — поставить рекламу на 3 суток без оплаты в боте (продал за рубли) — потом пришли текст\n"
            "/reject 5 причина — отклонить заявку с причиной\n"
            "/drop 5 — снять рекламу (без возврата) · /refund 5 — снять и вернуть звёзды\n"
            "/close · /open — пауза продаж / открыть\n"
            "/reply 123456 текст — ответить человеку · /stats — деньги · /post — перевыложить служебный пост\n"
            "Как покупатель: /start /buy /my")

    adm_help = adm_admin

    async def adm_queue(self, arg):
        act = [o for o in self.st["orders"] if o["status"] in ("review", "approved")
               or (o["status"] == "paid" and o["end"] >= self.today().isoformat())]
        await self.to_admin("Сейчас:\n" + ("\n".join(self._line(o) for o in act) if act else "пусто")
                            + f"\n\nСвободно с {d(self.free_from())} · продажа {'открыта' if self.st['open'] else 'на паузе'}")

    async def adm_price(self, arg):
        if not arg.isdigit() or int(arg) < 1:
            return await self.to_admin("/price 50 — сколько ⭐ за сутки")
        self.st["price"] = int(arg)
        self.save()
        await self.to_admin(f"Цена: {arg} ⭐ за сутки (для новых заявок).")

    async def adm_add(self, arg):
        if not arg.isdigit() or not 1 <= int(arg) <= 365:
            return await self.to_admin("/add 3 — на сколько суток, потом пришли текст рекламы")
        self.users[self.admin] = {"step": "admin_text", "days": int(arg)}
        await self.to_admin(f"Пришли текст рекламы на {dn(int(arg))} (до {AD_MAX} символов). Встанет в очередь с {d(self.free_from())}.")

    async def adm_reject(self, arg):
        m = re.match(r"(\d+)\s*(.*)", arg, re.S)
        o = m and self.order(int(m.group(1)))
        if not o or o["status"] not in ("review", "approved"):
            return await self.to_admin("/reject 5 причина — номер заявки на проверке")
        await self.reject(o, m.group(2).strip())

    async def adm_drop(self, arg, refund=False):
        o = self.order(int(arg)) if arg.isdigit() else None
        if not o or o["status"] not in ("paid", "approved", "review"):
            return await self.to_admin(("/refund" if refund else "/drop") + " 5 — номер заказа из /queue")
        if refund:
            if not o.get("charge_id"):
                return await self.to_admin("Этот заказ не оплачивали звёздами — возвращать нечего. /drop " + arg)
            try:
                await self.api("refundStarPayment", user_id=o["user_id"], telegram_payment_charge_id=o["charge_id"])
            except ApiError as e:
                return await self.to_admin(f"Возврат не прошёл: {e.desc}")
            self.st["earned"] -= o["price"]
            await self.send(o["user_id"], f"↩️ Звёзды за рекламу №{o['id']} возвращены.")
        o["status"] = "refunded" if refund else "cancelled"
        self.save()
        await self.to_admin(f"Заказ №{o['id']} {'возвращён' if refund else 'снят'}. Служебный пост обновится сейчас.")
        await self.sync()

    async def adm_refund(self, arg):
        await self.adm_drop(arg, refund=True)

    async def adm_refund_id(self, arg):
        p = arg.split()
        if len(p) != 2 or not p[0].isdigit():
            return await self.to_admin("/refund_id user_id charge_id")
        try:
            await self.api("refundStarPayment", user_id=int(p[0]), telegram_payment_charge_id=p[1])
            await self.to_admin("Возвращено.")
        except ApiError as e:
            await self.to_admin(f"Возврат не прошёл: {e.desc}")

    async def adm_close(self, arg):
        self.st["open"] = False
        self.save()
        await self.to_admin("⏸ Продажа на паузе. Оплаченные показы идут дальше. /open — открыть")

    async def adm_open(self, arg):
        self.st["open"] = True
        self.save()
        await self.to_admin("▶️ Продажа открыта.")

    async def adm_reply(self, arg):
        m = re.match(r"(\d+)\s+(.+)", arg, re.S)
        if not m:
            return await self.to_admin("/reply 123456 текст")
        r = await self.send(int(m.group(1)), "Ответ владельца: " + m.group(2))
        await self.to_admin("Отправил." if r else "Не дошло (человек заблокировал бота?).")

    async def adm_stats(self, arg):
        paid = [o for o in self.st["orders"] if o["status"] == "paid" and o.get("charge_id")]
        await self.to_admin(f"Заработано: {self.st['earned']} ⭐ (≈ ${self.st['earned'] * 0.013:.0f} при выводе)\n"
                            f"Оплаченных заказов: {len(paid)} · всего заявок: {len(self.st['orders'])}\n"
                            "Вывод: профиль бота → Звёзды/Баланс → Fragment (через 21 день, от 1000 ⭐).")

    async def adm_post(self, arg):
        ok = await self.sync(force=True)
        await self.to_admin("Служебный пост перевыложен." if ok else "Не получилось — бот админ канала с правом публиковать?")



# ───────────────────────── запуск ─────────────────────────

async def amain():
    cfg = get_config()
    if not cfg.get("ADS_BOT_TOKEN"):
        log.error("Нет ADS_BOT_TOKEN — создай бота в @BotFather и добавь токен в переменные или config.env.")
        return 1
    api = BotAPI(cfg["ADS_BOT_TOKEN"].strip())
    try:
        me = await api("getMe")
    except ApiError as e:
        log.error("Токен не подходит: %s", e.desc)
        return 2
    shop = Shop(api, cfg.get("ADMIN_ID"), int(cfg.get("PRICE") or 50))
    shop.username = me["username"]
    src = "файл" if shop.load() else ("бэкап из чата" if await shop.restore() else "новые")
    log.info("магазин рекламы @%s запущен; данные: %s; админ: %s", me["username"], src, shop.admin or "НЕ ЗАДАН")
    await shop._quiet("deleteWebhook")
    await shop._quiet("setMyCommands", commands=[
        {"command": "start", "description": "Реклама в Метрономе"}, {"command": "buy", "description": "Купить рекламу"},
        {"command": "my", "description": "Мои заказы"}, {"command": "paysupport", "description": "Вопрос по оплате"},
        {"command": "terms", "description": "Условия"}])
    await shop.to_admin(f"⏱ Магазин рекламы запущен. Команды владельца: /admin")

    async def ticker():
        while True:
            try:
                await shop.tick()
            except Exception:
                log.exception("тик")
            await asyncio.sleep(SYNC_EVERY)

    asyncio.create_task(ticker())
    offset = 0
    while True:
        try:
            ups = await api("getUpdates", offset=offset, timeout=50,
                            allowed_updates=["message", "callback_query", "pre_checkout_query", "channel_post"])
        except ApiError as e:
            if e.code == 401:
                log.error("Токен отозван — возьми новый в @BotFather.")
                return 2
            log.warning("getUpdates: %s", e.desc)  # 409 — бот запущен где-то ещё
            await asyncio.sleep(10)
            continue
        except Exception as e:
            log.warning("сеть: %s", e)
            await asyncio.sleep(5)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            try:
                await shop.handle(u)
            except Exception:
                log.exception("обработка")


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%d.%m %H:%M:%S", stream=sys.stdout)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass


if __name__ == "__main__":
    setup_logging()
    sys.exit(asyncio.run(amain()) or 0)
