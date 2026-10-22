#!/usr/bin/env bash
# Установка price_monitor на чистый сервер Ubuntu 24.04. Запускать от root:
#     git clone https://github.com/SHUHER322/price_monitor
#     bash price_monitor/deploy/setup.sh
# Повторный запуск безопасен: уже сделанные шаги пропускаются, код обновляется.
set -euo pipefail

APP_USER=monitor                       # бот работает не от root: Chrome от root не запускается
APP_DIR=/home/$APP_USER/price_monitor
REPO=https://github.com/SHUHER322/price_monitor.git
UV=/home/$APP_USER/.local/bin/uv

step() { echo; echo "=== $*"; }
as_app() { sudo -u "$APP_USER" -H bash -c "$1"; }

step "1/7 Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
# xvfb — виртуальный экран: на сервере нет монитора, а Chrome нужен с окном
# pipx — чтобы поставить uv с PyPI: сайт astral.sh с российских серверов не открывается
apt-get install -y -q git curl ca-certificates xvfb xauth fonts-liberation fonts-dejavu-core \
    pipx python3-venv

step "2/7 Google Chrome"
if ! command -v google-chrome >/dev/null; then
    curl -fsSL -o /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y -q /tmp/chrome.deb
    rm -f /tmp/chrome.deb
fi
google-chrome --version

step "3/7 Файл подкачки 2 ГБ (запас памяти для Chrome)"
if ! swapon --show | grep -q /swapfile; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q /swapfile /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi
free -h | head -3

step "4/7 Пользователь $APP_USER и код с GitHub"
id "$APP_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$APP_USER"
if [ -d "$APP_DIR/.git" ]; then
    as_app "git -C $APP_DIR pull --ff-only"
else
    as_app "git clone -q $REPO $APP_DIR"
fi

step "5/7 Python и библиотеки"
[ -x "$UV" ] || as_app "pipx install uv"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    # Python 3.14 uv качает с GitHub; если не вышло — системный Python 3.12 (бот работает и на нём)
    if ! as_app "cd $APP_DIR && rm -rf .venv && UV_HTTP_TIMEOUT=120 $UV venv -q --python 3.14 .venv"; then
        echo "Python 3.14 не скачался — беру системный: $(python3 -V)"
        as_app "cd $APP_DIR && rm -rf .venv && $UV venv -q --python /usr/bin/python3 .venv"
    fi
fi
as_app "cd $APP_DIR && UV_HTTP_TIMEOUT=120 $UV pip install -q -p .venv -r requirements.txt"
as_app "$APP_DIR/.venv/bin/python --version"

step "6/7 Настройки (.env)"
if [ -f "$APP_DIR/.env" ]; then
    echo ".env уже есть — не трогаю"
else
    read -rp "BOT_TOKEN (от @BotFather): " token </dev/tty
    read -rp "Твой Telegram ID (ALLOWED_USERS): " users </dev/tty
    sed -e "s|^BOT_TOKEN=.*|BOT_TOKEN=$token|" \
        -e "s|^ALLOWED_USERS=.*|ALLOWED_USERS=$users|" \
        "$APP_DIR/.env.example" > "$APP_DIR/.env"
    chown "$APP_USER:" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo ".env создан"
fi

step "7/7 Служба systemd (автозапуск после перезагрузки)"
cp "$APP_DIR/deploy/price-monitor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q price-monitor
echo "Служба установлена, но пока не запущена."

echo
echo "Готово. Дальше:"
echo "  1) проверка WB и Ozon:  bash $APP_DIR/deploy/check.sh"
echo "  2) запуск бота:         systemctl start price-monitor"
echo "  3) логи бота:           journalctl -u price-monitor -f"
