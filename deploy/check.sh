#!/usr/bin/env bash
# Проверка с сервера: пускают ли WB и Ozon (через Chrome на виртуальном экране).
# Запускать от root, бот при этом должен быть остановлен (один профиль браузера):
#     bash /home/monitor/price_monitor/deploy/check.sh
set -uo pipefail

APP_DIR=/home/monitor/price_monitor
run() { sudo -u monitor -H bash -c "cd $APP_DIR && mkdir -p data && xvfb-run -a .venv/bin/python -m $1"; }

if systemctl is-active -q price-monitor; then
    echo "Бот запущен — останови его на время проверки: systemctl stop price-monitor"
    exit 1
fi

echo "=== Wildberries (около минуты: получаем антибот-токен)"
run "app.sources.wb 683146715" && wb=OK || wb=ОШИБКА

echo
echo "=== Ozon (около 15 секунд)"
run "app.sources.ozon 1291633069" && oz=OK || oz=ОШИБКА

echo
echo "Итог: WB — $wb, Ozon — $oz"
