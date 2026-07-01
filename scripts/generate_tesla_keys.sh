#!/usr/bin/env bash
set -euo pipefail

directory="${1:-.secrets/tesla}"
mkdir -p "$directory"
umask 077

if [[ -e "$directory/private-key.pem" ]]; then
  echo "La chiave $directory/private-key.pem esiste già; non la sovrascrivo." >&2
  exit 1
fi

openssl ecparam -name prime256v1 -genkey -noout -out "$directory/private-key.pem"
openssl ec -in "$directory/private-key.pem" -pubout -out "$directory/public-key.pem"

chmod 600 "$directory/private-key.pem"
chmod 644 "$directory/public-key.pem"
echo "Chiavi create in $directory. La chiave privata non deve mai uscire da questa macchina."
