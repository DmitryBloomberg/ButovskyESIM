#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="esim-telegram-bot"
INSTALL_DIR="${ESIM_BOT_INSTALL_DIR:-/opt/esim-telegram-bot}"
ENV_FILE="/etc/esim-telegram-bot.env"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="esim-bot"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "Нужны права root для установки systemd-службы. Установите sudo или запустите от root." >&2
    exit 1
  fi
  exec sudo -- bash "$0" "$@"
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl не найден. Запускайте этот скрипт на Linux-сервере с systemd." >&2
  exit 1
fi

if [[ ! -f "${SCRIPT_DIR}/bot.py" ]]; then
  echo "Не найден ${SCRIPT_DIR}/bot.py" >&2
  exit 1
fi

if [[ "${INSTALL_DIR}" =~ [[:space:]] ]]; then
  echo "Путь установки не должен содержать пробелы: ${INSTALL_DIR}" >&2
  exit 1
fi

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "Python 3 не найден. Установите Python 3.10 или новее и повторите запуск." >&2
  exit 1
fi
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Требуется Python 3.10 или новее." >&2
  exit 1
fi

read -r -s -p "Токен Telegram-бота (ввод скрыт): " BOT_TOKEN
echo
if [[ ! "${BOT_TOKEN}" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  echo "Формат токена не распознан. Получите токен у BotFather и запустите скрипт снова." >&2
  unset BOT_TOKEN
  exit 1
fi

read -r -p "Telegram ID администратора: " ADMIN_ID
if [[ ! "${ADMIN_ID}" =~ ^[0-9]+$ ]] || [[ "${ADMIN_ID}" == "0" ]]; then
  echo "Telegram ID должен быть положительным числом." >&2
  unset BOT_TOKEN ADMIN_ID
  exit 1
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  if ! command -v useradd >/dev/null 2>&1; then
    echo "Команда useradd не найдена; создайте системного пользователя ${SERVICE_USER} вручную." >&2
    unset BOT_TOKEN ADMIN_ID
    exit 1
  fi
  useradd --system --home-dir "${INSTALL_DIR}" --no-create-home \
    --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${INSTALL_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${INSTALL_DIR}/data"
install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0640 \
  "${SCRIPT_DIR}/bot.py" "${INSTALL_DIR}/bot.py"

ENV_TMP="$(mktemp /etc/esim-telegram-bot.env.XXXXXX)"
cleanup() {
  rm -f "${ENV_TMP}"
  unset BOT_TOKEN ADMIN_ID
}
trap cleanup EXIT
chmod 0600 "${ENV_TMP}"
printf 'BOT_TOKEN=%s\nADMIN_ID=%s\n' "${BOT_TOKEN}" "${ADMIN_ID}" > "${ENV_TMP}"
install -o root -g root -m 0600 "${ENV_TMP}" "${ENV_FILE}"
unset BOT_TOKEN ADMIN_ID

cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=Telegram eSIM sales bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
Environment=BOT_DATA_DIR=${INSTALL_DIR}/data
ExecStart=${PYTHON_BIN} ${INSTALL_DIR}/bot.py
Restart=always
RestartSec=5
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}/data

[Install]
WantedBy=multi-user.target
EOF

chmod 0644 "${UNIT_FILE}"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

echo
echo "Бот установлен и запущен как systemd-служба: ${SERVICE_NAME}"
echo "Автозапуск после перезагрузки включён."
echo "Состояние: systemctl status ${SERVICE_NAME}"
echo "Журнал:    journalctl -u ${SERVICE_NAME} -f"