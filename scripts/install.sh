#!/bin/bash
set -euo pipefail
INSTALL_DIR="/opt/kisa"
DATA_DIR="/srv/kisa"
REPO_URL="${REPO_URL:-https://github.com/dragunovsky/odesa-test.git}"

[ "$EUID" -eq 0 ] || { echo "Запусти від root"; exit 1; }

id kisa &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin kisa
mkdir -p "$INSTALL_DIR" "$DATA_DIR/bundles" /etc/kisa

# код КІСА
rsync -a --exclude='.venv' "$(dirname "$0")/../" "$INSTALL_DIR/"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q flask pyyaml gunicorn

# локальний clone ОДЕСА для build бандлів
[ -d "$DATA_DIR/odesa-repo/.git" ] || git clone "$REPO_URL" "$DATA_DIR/odesa-repo"

# конфіг
[ -f /etc/kisa/kisa.yml ] || cp "$INSTALL_DIR/config/kisa.yml.example" /etc/kisa/kisa.yml
chown -R kisa:kisa "$INSTALL_DIR" "$DATA_DIR" /etc/kisa

cp "$INSTALL_DIR/scripts/systemd/kisa.service" /etc/systemd/system/
systemctl daemon-reload
echo "Готово. Відредагуй /etc/kisa/kisa.yml, потім: systemctl enable --now kisa"
