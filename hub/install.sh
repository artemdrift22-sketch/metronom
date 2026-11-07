#!/usr/bin/env bash
# Метроном · установка общего бота на VPS (Ubuntu 22.04+ / Debian 12+). Повторный запуск = обновление.
#   curl -fsSL https://raw.githubusercontent.com/GITHUB_USER/metronom/main/hub/install.sh | sudo bash -s GITHUB_USER/metronom
set -euo pipefail
REPO="${1:-${REPO:-}}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/metronom-hub}"
SVC=metronom-hub

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "$REPO" ] || die "Укажи репозиторий:  ... | sudo bash -s ТВОЙ_GITHUB/metronom"
[ "$(id -u)" = 0 ] || die "Нужны права root: добавь sudo перед bash"

say "Ставлю Python"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null
  apt-get install -y -qq python3 curl ca-certificates >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 curl ca-certificates >/dev/null
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Нужен Python 3.10 или новее — поставь Ubuntu 22.04/24.04"

say "Скачиваю бота из github.com/$REPO"
mkdir -p "$DIR"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH/hub"
for f in hub.py ads_bot.py; do
  curl -fsSL "$RAW/$f" -o "$DIR/$f.new" || die "Не скачался $RAW/$f — репозиторий публичный? Файлы лежат в папке hub?"
  mv "$DIR/$f.new" "$DIR/$f"
done
python3 -m py_compile "$DIR/hub.py" "$DIR/ads_bot.py" || die "Файлы бота битые — перезалей их в GitHub"

id metronom >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin metronom

if [ ! -f "$DIR/config.env" ]; then
  TOKEN="${BOT_TOKEN:-}"
  while true; do
    if [ -z "$TOKEN" ]; then
      [ -r /dev/tty ] || die "Нет терминала. Запусти так: BOT_TOKEN=123:ABC ... | sudo -E bash -s $REPO"
      read -r -p "Вставь токен бота от @BotFather (вида 123456:ABC...): " TOKEN < /dev/tty
    fi
    TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
    if printf '%s' "$TOKEN" | grep -Eq '^[0-9]{5,}:[A-Za-z0-9_-]{30,}$' \
       && curl -fsS "https://api.telegram.org/bot$TOKEN/getMe" 2>/dev/null | grep -q '"ok":true'; then
      break
    fi
    echo "Токен не подходит — скопируй его из @BotFather целиком и вставь ещё раз."
    TOKEN=""
  done
  CODE="$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
  ( umask 077; printf 'BOT_TOKEN=%s\nREPO=%s\nADMIN_CODE=%s\nPRICE=50\n' "$TOKEN" "$REPO" "$CODE" > "$DIR/config.env" )
else
  sed -i "s#^REPO=.*#REPO=$REPO#" "$DIR/config.env"
  CODE="$(grep -E '^ADMIN_CODE=' "$DIR/config.env" | cut -d= -f2- || true)"
fi
chown -R metronom:metronom "$DIR"
chmod 600 "$DIR/config.env"

say "Запускаю службу $SVC"
cat > "${UNIT_DIR:-/etc/systemd/system}/$SVC.service" <<UNIT
[Unit]
Description=Metronom hub bot
After=network-online.target
Wants=network-online.target

[Service]
User=metronom
WorkingDirectory=$DIR
ExecStart=/usr/bin/env python3 -u $DIR/hub.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 "${UNIT_DIR:-/etc/systemd/system}/$SVC.service"
systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1
systemctl restart "$SVC"
sleep 5
if ! systemctl is-active --quiet "$SVC"; then
  journalctl -u "$SVC" -n 30 --no-pager || true
  die "Бот не запустился — выше лог"
fi
BOTNAME="$(journalctl -u "$SVC" -n 50 --no-pager 2>/dev/null | grep -o 'бот @[A-Za-z0-9_]*' | tail -1 || true)"

printf '\n\033[1;32m✔ Готово: %s работает и сам перезапускается.\033[0m\n' "${BOTNAME:-бот}"
if ! grep -q '^ADMIN_ID=' "$DIR/config.env"; then
  printf '\nПоследний шаг — напиши своему боту в Telegram:\n\n    /admin %s\n\n(так бот запомнит, что владелец — ты)\n' "$CODE"
fi
cat <<TXT

Полезное:
  журнал:      journalctl -u $SVC -f
  перезапуск:  systemctl restart $SVC
  обновить:    запусти эту же команду установки ещё раз
  настройки:   $DIR/config.env
TXT
