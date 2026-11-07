#!/usr/bin/env bash
# Метроном · Страж — установка бота-модератора на VPS (Ubuntu 22.04+ / Debian 11+). Повторный запуск = обновление.
#   curl -fsSL https://raw.githubusercontent.com/GITHUB_USER/metronom/main/guard/install.sh | sudo bash -s GITHUB_USER/metronom
set -euo pipefail
REPO="${1:-${REPO:-}}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/metronom-guard}"
SVC=metronom-guard

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "$REPO" ] || die "Укажи репозиторий:  ... | sudo bash -s ТВОЙ_GITHUB/metronom"
[ "$(id -u)" = 0 ] || die "Нужны права root: добавь sudo перед bash"

say "Ставлю Python"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null
  apt-get install -y -qq python3 curl ca-certificates tzdata >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 curl ca-certificates tzdata >/dev/null
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Нужен Python 3.9 или новее — поставь Ubuntu 22.04/24.04"

say "Скачиваю бота из github.com/$REPO"
mkdir -p "$DIR"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH/guard"
curl -fsSL "$RAW/guard.py" -o "$DIR/guard.py.new" || die "Не скачался $RAW/guard.py — репозиторий публичный? Есть папка guard?"
python3 -m py_compile "$DIR/guard.py.new" || die "Файл бота битый — перезалей guard/guard.py в GitHub"
mv "$DIR/guard.py.new" "$DIR/guard.py"

id metronom >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin metronom

NEWPW=""
if [ ! -f "$DIR/config.env" ]; then
  TOKEN="${BOT_TOKEN:-}"
  while true; do
    if [ -z "$TOKEN" ]; then
      [ -r /dev/tty ] || die "Нет терминала. Запусти так: BOT_TOKEN=123:ABC PASSWORD=... ... | sudo -E bash -s $REPO"
      read -r -p "Вставь токен НОВОГО бота-модератора от @BotFather (вида 123456:ABC...): " TOKEN < /dev/tty
    fi
    TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
    if printf '%s' "$TOKEN" | grep -Eq '^[0-9]{5,}:[A-Za-z0-9_-]{30,}$' \
       && curl -fsS "https://api.telegram.org/bot$TOKEN/getMe" 2>/dev/null | grep -q '"ok":true'; then
      break
    fi
    echo "Токен не подходит — скопируй его из @BotFather целиком и вставь ещё раз."
    TOKEN=""
  done
  PW="${PASSWORD:-}"
  if [ -z "$PW" ] && [ -r /dev/tty ]; then
    read -r -p "Придумай пароль для входа в панель (от 8 символов; Enter — придумаю сам): " PW < /dev/tty
  fi
  PW="$(printf '%s' "$PW" | tr -d '[:space:]')"
  if [ "${#PW}" -lt 8 ]; then
    [ -z "$PW" ] || echo "Короче 8 символов — придумаю надёжный сам."
    PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
  fi
  NEWPW="$PW"
  ( umask 077; printf 'BOT_TOKEN=%s\nPASSWORD=%s\nTZ=%s\n' "$TOKEN" "$PW" "${TZ_NAME:-Asia/Yekaterinburg}" > "$DIR/config.env" )
fi
chown -R metronom:metronom "$DIR"
chmod 600 "$DIR/config.env"

say "Запускаю службу $SVC"
cat > "${UNIT_DIR:-/etc/systemd/system}/$SVC.service" <<UNIT
[Unit]
Description=Metronom Guard moderation bot
After=network-online.target
Wants=network-online.target

[Service]
User=metronom
WorkingDirectory=$DIR
ExecStart=/usr/bin/env python3 -u $DIR/guard.py
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
BOTNAME="$(journalctl -u "$SVC" -n 50 --no-pager 2>/dev/null | grep -o '@[A-Za-z0-9_]*[bB][oO][tT]' | tail -1 || true)"

printf '\n\033[1;32m✔ Готово: Страж %s работает и сам перезапускается.\033[0m\n' "${BOTNAME:-}"
if [ -n "$NEWPW" ]; then
  printf '\nНапиши своему боту в Telegram:\n\n    /login\n\nи пришли пароль:  \033[1m%s\033[0m\n(запиши его; поменять можно в боте: ⚙️ Настройки → 🔑 Сменить пароль)\n' "$NEWPW"
fi
cat <<TXT

Полезное:
  журнал:          journalctl -u $SVC -f
  перезапуск:      systemctl restart $SVC
  обновить:        запусти эту же команду установки ещё раз (данные и настройки сохранятся)
  забыл пароль:    sudo -u metronom python3 $DIR/guard.py --password НОВЫЙ_ПАРОЛЬ && systemctl restart $SVC
  база и бэкапы:   $DIR/guard.db (бот сам присылает бэкап раз в сутки)
TXT
