#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Eseguire con sudo: sudo bash scripts/provision_raspberry_pi.sh BINARY_ARM64" >&2
  exit 1
fi
if [[ "$#" -ne 1 || ! -x "$1" ]]; then
  echo "Uso: sudo bash scripts/provision_raspberry_pi.sh /percorso/tesla-control-linux-arm64" >&2
  exit 1
fi
if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "Errore: serve Raspberry Pi OS 64-bit (architettura aarch64)" >&2
  exit 1
fi

apt-get update
apt-get install -y --no-install-recommends bluetooth bluez libcap2-bin python3-venv
systemctl enable --now bluetooth.service systemd-timesyncd.service

if ! id tesla-energy >/dev/null 2>&1; then
  useradd --system --home-dir /opt/tesla-energy-controller --shell /usr/sbin/nologin tesla-energy
fi
usermod -aG bluetooth tesla-energy
install -m 0755 "$1" /usr/local/bin/tesla-control
setcap 'cap_net_admin=eip' /usr/local/bin/tesla-control
install -d -o tesla-energy -g tesla-energy -m 0750 \
  /opt/tesla-energy-controller \
  /opt/tesla-energy-controller/data \
  /opt/tesla-energy-controller/.secrets/tesla

echo "Provisioning BLE completato. Verificare: getcap /usr/local/bin/tesla-control"
echo "Nota: setcap va rieseguito ogni volta che il binario viene sostituito."
