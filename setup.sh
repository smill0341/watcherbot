#!/bin/bash
# Установка бота на сервер (Ubuntu 24.04). Запуск: bash setup.sh
# Настройки — здесь:
LOGIN="watcherbotomd"                  # логин для входа в веб-интерфейс
PASSWORD="WatcherOMD888"
PROJECT_DIR="/root/master_bot"      # папка, в которой лежит backend/ (внутри него run_web.py)

set -e
if [ "$PASSWORD" = "ПОМЕНЯЙ_МЕНЯ" ]; then echo "Сначала поменяй PASSWORD в начале файла"; exit 1; fi
if [ ! -f "$PROJECT_DIR/backend/run_web.py" ]; then echo "Не найден $PROJECT_DIR/backend/run_web.py"; exit 1; fi

apt-get update
apt-get install -y python3-venv python3-pip ufw curl debian-keyring debian-archive-keyring apt-transport-https gpg
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
  apt-get update && apt-get install -y caddy
fi

cd "$PROJECT_DIR"
python3 -m venv venv
venv/bin/pip install -r backend/requirements.txt
find "$PROJECT_DIR" -name trade_keys.json -exec chmod 600 {} \;

cat > /etc/systemd/system/watcherbot.service <<UNIT
[Unit]
Description=watcherbot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$PROJECT_DIR
ExecStartPre=/bin/rm -f $PROJECT_DIR/backend/run_web.pid
ExecStart=$PROJECT_DIR/venv/bin/python -m backend.run_web
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

IP=$(curl -s -4 ifconfig.me)
HASH=$(caddy hash-password --plaintext "$PASSWORD")
cat > /etc/caddy/Caddyfile <<CADDY
https://$IP {
    tls internal
    basic_auth {
        $LOGIN $HASH
    }
    reverse_proxy 127.0.0.1:8010
}
CADDY

ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

systemctl daemon-reload
systemctl enable --now watcherbot
systemctl restart caddy

echo
echo "Готово. Открой: https://$IP  (браузер покажет предупреждение о сертификате — это нормально, Дополнительно -> Продолжить)"
echo "Логи бота: journalctl -u watcherbot -f"
