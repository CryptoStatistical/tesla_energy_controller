#!/usr/bin/env bash
set -euo pipefail

key_dir="${TESLA_KEY_DIR:-.secrets/tesla}"
public_key="$key_dir/public-key.pem"

if ! command -v tesla-control >/dev/null 2>&1; then
  echo "tesla-control non trovato nel PATH." >&2
  exit 1
fi

if [[ ! -f "$public_key" ]]; then
  bash scripts/generate_tesla_keys.sh "$key_dir"
fi

vin="${TESLA_VIN:-}"
if [[ -z "$vin" ]]; then
  read -r -p "VIN Tesla: " vin
fi
if [[ -z "$vin" ]]; then
  echo "VIN obbligatorio." >&2
  exit 1
fi

echo "Avvicina il dispositivo all'auto e tieni pronta la chiave NFC."
tesla-control \
  -ble \
  -vin "$vin" \
  -connect-timeout 60s \
  add-key-request "$public_key" charging_manager cloud_key

echo "Conferma ora l'aggiunta appoggiando la chiave NFC sulla console centrale."
