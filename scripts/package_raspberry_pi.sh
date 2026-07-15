#!/usr/bin/env bash
set -euo pipefail

platform="${PACKAGE_PLATFORM:-linux/arm64}"
python_image="${PACKAGE_PYTHON_IMAGE:-python:3.11-slim-bookworm}"
package_name="${PACKAGE_NAME:-tesla-energy-controller-raspberry-arm64}"
output="${1:-dist/${package_name}.tar.gz}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Errore: serve Docker per creare wheel Linux ARM64 riproducibili." >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

root="$tmpdir/$package_name"
mkdir -p "$root/wheels" "$root/deploy" "$root/scripts" "$root/bin"

docker run --rm --platform "$platform" \
  -v "$PWD:/src:ro" \
  -v "$root/wheels:/out" \
  -w /src \
  "$python_image" \
  sh -c "python -m pip install --upgrade pip >/dev/null && python -m pip wheel --wheel-dir /out ."

cp README.md .env.example "$root/"
cp deploy/tesla-energy-controller.service "$root/deploy/"
cp deploy/tesla-energy-controller-network.service "$root/deploy/"
cp deploy/tesla-energy-controller-network-watchdog.service \
  deploy/tesla-energy-controller-network-watchdog.timer \
  deploy/tesla-energy-controller-journald.conf \
  "$root/deploy/"
cp scripts/generate_tesla_keys.sh \
  scripts/pair_tesla_ble.sh \
  scripts/provision_raspberry_pi.sh \
  scripts/build_tesla_control_arm64.sh \
  scripts/provision_vimar_raspberry_pi.sh \
  scripts/bootstrap_raspberry_network.sh \
  scripts/raspberry_network_watchdog.sh \
  "$root/scripts/"

if [[ -x dist/tesla-control-linux-arm64 ]]; then
  cp dist/tesla-control-linux-arm64 "$root/bin/tesla-control"
fi

cat >"$root/install_on_raspberry_pi.sh" <<'INSTALLER'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Eseguire con sudo: sudo ./install_on_raspberry_pi.sh" >&2
  exit 1
fi
if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "Errore: questo bundle è per Raspberry Pi OS 64-bit / aarch64." >&2
  exit 1
fi

bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="/opt/tesla-energy-controller"
app_user="tesla-energy"

apt-get update
apt-get install -y --no-install-recommends \
  bluetooth bluez iproute2 iputils-ping iw libcap2-bin network-manager openssl python3-venv util-linux
systemctl enable --now bluetooth.service systemd-timesyncd.service

if ! id "$app_user" >/dev/null 2>&1; then
  useradd --system --home-dir "$app_dir" --shell /usr/sbin/nologin "$app_user"
fi
usermod -aG bluetooth "$app_user"

install -d -o "$app_user" -g "$app_user" -m 0750 \
  "$app_dir" \
  "$app_dir/data" \
  "$app_dir/.secrets" \
  "$app_dir/.secrets/tesla"

python3 -m venv "$app_dir/.venv"
"$app_dir/.venv/bin/python" -m pip install --upgrade pip
"$app_dir/.venv/bin/python" -m pip install --no-index --find-links "$bundle_dir/wheels" tesla-energy-controller

if [[ ! -f "$app_dir/.env" ]]; then
  install -o "$app_user" -g "$app_user" -m 0600 "$bundle_dir/.env.example" "$app_dir/.env"
fi

if [[ -x "$bundle_dir/bin/tesla-control" ]]; then
  install -m 0755 "$bundle_dir/bin/tesla-control" /usr/local/bin/tesla-control
  setcap 'cap_net_admin=eip' /usr/local/bin/tesla-control
else
  echo "Nota: tesla-control non è incluso. Copiarlo in /usr/local/bin/tesla-control e applicare setcap." >&2
fi

install -m 0644 "$bundle_dir/deploy/tesla-energy-controller.service" \
  /etc/systemd/system/tesla-energy-controller.service
install -m 0644 "$bundle_dir/deploy/tesla-energy-controller-network.service" \
  /etc/systemd/system/tesla-energy-controller-network.service
install -m 0644 "$bundle_dir/deploy/tesla-energy-controller-network-watchdog.service" \
  /etc/systemd/system/tesla-energy-controller-network-watchdog.service
install -m 0644 "$bundle_dir/deploy/tesla-energy-controller-network-watchdog.timer" \
  /etc/systemd/system/tesla-energy-controller-network-watchdog.timer
install -m 0755 "$bundle_dir/scripts/bootstrap_raspberry_network.sh" \
  /usr/local/sbin/tesla-energy-controller-network
install -m 0755 "$bundle_dir/scripts/raspberry_network_watchdog.sh" \
  /usr/local/sbin/tesla-energy-controller-network-watchdog
install -d -m 0755 /etc/systemd/journald.conf.d /var/log/journal
install -m 0644 "$bundle_dir/deploy/tesla-energy-controller-journald.conf" \
  /etc/systemd/journald.conf.d/tesla-energy-controller.conf
if [[ ! -f /etc/default/tesla-energy-controller-network ]]; then
  cat >/etc/default/tesla-energy-controller-network <<'NETWORK'
NETWORK_DEVICE=wlan0
WIFI_RESILIENCE_ENABLED=true
WIFI_CONNECTION_NAME=
WIFI_GATEWAY=192.168.1.1
WIFI_FAILURE_THRESHOLD=3
WIFI_PING_COUNT=2
WIFI_PING_TIMEOUT_SECONDS=2
WIFI_RECOVERY_WAIT_SECONDS=20
STOREDGE_ROUTE_ENABLED=true
STOREDGE_ROUTE_CIDR=192.168.2.0/24
STOREDGE_ROUTE_GATEWAY=192.168.1.61
ALFA_NEIGHBOR_ENABLED=true
ALFA_NEIGHBOR_IP=192.168.1.169
ALFA_NEIGHBOR_MAC=34:ab:95:5a:be:68
ALFA_MODBUS_PORT=502
NETWORK
fi
grep -q '^WIFI_RESILIENCE_ENABLED=' /etc/default/tesla-energy-controller-network || echo 'WIFI_RESILIENCE_ENABLED=true' >>/etc/default/tesla-energy-controller-network
grep -q '^WIFI_CONNECTION_NAME=' /etc/default/tesla-energy-controller-network || echo 'WIFI_CONNECTION_NAME=' >>/etc/default/tesla-energy-controller-network
grep -q '^WIFI_GATEWAY=' /etc/default/tesla-energy-controller-network || echo 'WIFI_GATEWAY=192.168.1.1' >>/etc/default/tesla-energy-controller-network
grep -q '^WIFI_FAILURE_THRESHOLD=' /etc/default/tesla-energy-controller-network || echo 'WIFI_FAILURE_THRESHOLD=3' >>/etc/default/tesla-energy-controller-network
grep -q '^WIFI_PING_COUNT=' /etc/default/tesla-energy-controller-network || echo 'WIFI_PING_COUNT=2' >>/etc/default/tesla-energy-controller-network
grep -q '^WIFI_PING_TIMEOUT_SECONDS=' /etc/default/tesla-energy-controller-network || echo 'WIFI_PING_TIMEOUT_SECONDS=2' >>/etc/default/tesla-energy-controller-network
grep -q '^WIFI_RECOVERY_WAIT_SECONDS=' /etc/default/tesla-energy-controller-network || echo 'WIFI_RECOVERY_WAIT_SECONDS=20' >>/etc/default/tesla-energy-controller-network
chown -R "$app_user:$app_user" "$app_dir"
systemctl daemon-reload
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald.service
journalctl --flush
systemctl enable tesla-energy-controller-network-watchdog.timer

echo "Installazione completata in $app_dir"
echo "Prossimi passi:"
echo "  1. configurare $app_dir/.env"
echo "  2. generare/abbinare la chiave Tesla BLE"
echo "  3. avviare rete e controller con: sudo systemctl enable --now tesla-energy-controller-network tesla-energy-controller-network-watchdog.timer tesla-energy-controller"
INSTALLER
chmod 0755 "$root/install_on_raspberry_pi.sh"

mkdir -p "$(dirname "$output")"
tar -C "$tmpdir" -czf "$output" "$package_name"
echo "Creato $output"
