#!/usr/bin/env bash
set -euo pipefail

remote_host="${1:-192.168.1.200}"
remote_user="${2:-rasp26}"
bundle="${3:-dist/tesla-energy-controller-raspberry-arm64.tar.gz}"

if [[ ! -f "$bundle" ]]; then
  echo "Bundle non trovato: $bundle" >&2
  echo "Crealo prima con: bash scripts/package_raspberry_pi.sh" >&2
  exit 1
fi
if [[ ! -f ".secrets/vimar_private_key.pem" ]]; then
  echo "Chiave privata Vimar non trovata: .secrets/vimar_private_key.pem" >&2
  exit 1
fi
if [[ ! -f ".secrets/vimar_public_key.pem" ]]; then
  echo "Chiave pubblica Vimar non trovata: .secrets/vimar_public_key.pem" >&2
  exit 1
fi
if [[ ! -f ".secrets/VimarCA.cert.pem" ]]; then
  echo "Certificato CA Vimar non trovato: .secrets/VimarCA.cert.pem" >&2
  exit 1
fi

ssh_target="${remote_user}@${remote_host}"
remote_tmp="/tmp/tesla-energy-controller-raspberry-arm64.tar.gz"
control_path="$(mktemp -u "/tmp/tesla-energy-rpi-ssh.XXXXXX")"
ssh_opts=(
  -o StrictHostKeyChecking=accept-new
  -o ControlMaster=auto
  -o ControlPath="$control_path"
  -o ControlPersist=10m
)

cleanup() {
  ssh "${ssh_opts[@]}" -O exit "$ssh_target" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ssh "${ssh_opts[@]}" -Nf "$ssh_target" || true

scp "${ssh_opts[@]}" "$bundle" "$ssh_target:$remote_tmp"
ssh "${ssh_opts[@]}" "$ssh_target" "rm -rf /tmp/tesla-energy-controller-raspberry-arm64 && tar -xzf '$remote_tmp' -C /tmp"
ssh -tt "${ssh_opts[@]}" "$ssh_target" "cd /tmp/tesla-energy-controller-raspberry-arm64 && sudo ./install_on_raspberry_pi.sh"

ssh -tt "${ssh_opts[@]}" "$ssh_target" "sudo install -d -o tesla-energy -g tesla-energy -m 0750 /opt/tesla-energy-controller/.secrets"
scp "${ssh_opts[@]}" .secrets/vimar_private_key.pem "$ssh_target:/tmp/vimar_private_key.pem"
scp "${ssh_opts[@]}" .secrets/vimar_public_key.pem "$ssh_target:/tmp/vimar_public_key.pem"
scp "${ssh_opts[@]}" .secrets/VimarCA.cert.pem "$ssh_target:/tmp/VimarCA.cert.pem"
if [[ -f ".secrets/vimar_credentials.json" ]]; then
  scp "${ssh_opts[@]}" .secrets/vimar_credentials.json "$ssh_target:/tmp/vimar_credentials.json"
fi
ssh -tt "${ssh_opts[@]}" "$ssh_target" "sudo install -o tesla-energy -g tesla-energy -m 0600 /tmp/vimar_private_key.pem /opt/tesla-energy-controller/.secrets/vimar_private_key.pem"
ssh -tt "${ssh_opts[@]}" "$ssh_target" "sudo install -o tesla-energy -g tesla-energy -m 0644 /tmp/vimar_public_key.pem /opt/tesla-energy-controller/.secrets/vimar_public_key.pem"
ssh -tt "${ssh_opts[@]}" "$ssh_target" "sudo install -o tesla-energy -g tesla-energy -m 0644 /tmp/VimarCA.cert.pem /opt/tesla-energy-controller/.secrets/VimarCA.cert.pem"
ssh -tt "${ssh_opts[@]}" "$ssh_target" "if [ -f /tmp/vimar_credentials.json ]; then sudo install -o tesla-energy -g tesla-energy -m 0600 /tmp/vimar_credentials.json /opt/tesla-energy-controller/.secrets/vimar_credentials.json; fi"
ssh "${ssh_opts[@]}" "$ssh_target" "rm -f /tmp/vimar_private_key.pem /tmp/vimar_public_key.pem /tmp/VimarCA.cert.pem /tmp/vimar_credentials.json"

echo "Ambiente Vimar copiato su $ssh_target."
echo "Configura /opt/tesla-energy-controller/.env, poi esegui il pairing con:"
echo "  cd /opt/tesla-energy-controller && sudo -u tesla-energy bash -lc 'set -a; source .env; set +a; .venv/bin/tesla-energy-controller vimar-pair'"
