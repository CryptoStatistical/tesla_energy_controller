#!/usr/bin/env bash
set -euo pipefail

remote="${1:-rasp26@192.168.1.200}"
app_dir="${2:-/opt/tesla-energy-controller}"
service_user="${SERVICE_USER:-tesla-energy}"
stamp="$(date +%Y%m%d%H%M%S)"
stage="/tmp/tesla-energy-controller-deploy-$stamp"

cd "$(dirname "$0")/.."

echo "==> Local tests"
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .

echo "==> Remote staging: $remote:$stage"
ssh "$remote" "rm -rf '$stage' && mkdir -p '$stage'"
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.claude/' \
  --exclude '.env' \
  --exclude '.venv/' \
  --exclude '.secrets/' \
  --exclude 'data/' \
  --exclude 'dist/' \
  --exclude 'outputs/' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '__pycache__/' \
  ./ "$remote:$stage/"

echo "==> Install code, preserve .env/.secrets/data/.venv"
ssh -tt "$remote" "sudo bash -lc '
set -euo pipefail
mkdir -p \"$app_dir\" \"$app_dir/data\" \"$app_dir/.secrets\"
rsync -a --delete \
  --exclude \".env\" \
  --exclude \".venv/\" \
  --exclude \".secrets/\" \
  --exclude \"data/\" \
  \"$stage/\" \"$app_dir/\"
install -m 0644 \"$app_dir/deploy/tesla-energy-controller.service\" /etc/systemd/system/tesla-energy-controller.service
install -m 0644 \"$app_dir/deploy/tesla-energy-controller-tuya.service\" /etc/systemd/system/tesla-energy-controller-tuya.service
install -m 0644 \"$app_dir/deploy/tesla-energy-controller-network.service\" /etc/systemd/system/tesla-energy-controller-network.service
install -m 0755 \"$app_dir/scripts/bootstrap_raspberry_network.sh\" /usr/local/sbin/tesla-energy-controller-network
if [ ! -f /etc/default/tesla-energy-controller-network ]; then
  install -m 0644 /dev/null /etc/default/tesla-energy-controller-network
  cat >/etc/default/tesla-energy-controller-network <<EOF
NETWORK_DEVICE=wlan0
STOREDGE_ROUTE_ENABLED=true
STOREDGE_ROUTE_CIDR=192.168.2.0/24
STOREDGE_ROUTE_GATEWAY=192.168.1.61
ALFA_NEIGHBOR_ENABLED=true
ALFA_NEIGHBOR_IP=192.168.1.169
ALFA_NEIGHBOR_MAC=34:ab:95:5a:be:68
ALFA_MODBUS_PORT=502
EOF
fi
systemctl disable --now tesla-energy-controller-route.service alfa-static-neighbor.service 2>/dev/null || true
rm -f \
  /etc/systemd/system/tesla-energy-controller-route.service \
  /etc/default/tesla-energy-controller-route \
  /etc/systemd/system/alfa-static-neighbor.service
chown -R \"$service_user:$service_user\" \"$app_dir\"
chmod 600 \"$app_dir/.env\" 2>/dev/null || true
if [ ! -x \"$app_dir/.venv/bin/python\" ]; then
  python3 -m venv \"$app_dir/.venv\"
  chown -R \"$service_user:$service_user\" \"$app_dir/.venv\"
fi
sudo -u \"$service_user\" \"$app_dir/.venv/bin/python\" -m pip install -e \"$app_dir\"
systemctl daemon-reload
systemctl enable tesla-energy-controller-network.service tesla-energy-controller.service tesla-energy-controller-tuya.service
systemctl restart tesla-energy-controller-network.service
systemctl restart tesla-energy-controller.service
systemctl restart tesla-energy-controller-tuya.service
rm -rf \"$stage\"
'"

echo "==> Remote status"
ssh "$remote" "systemctl --no-pager --lines=0 status tesla-energy-controller-network.service tesla-energy-controller.service tesla-energy-controller-tuya.service"
ssh "$remote" "for i in 1 2 3 4 5 6 7 8 9 10; do curl -fsS http://127.0.0.1:8080/health && exit 0; sleep 1; done; exit 1"
echo
echo "Deploy complete."
