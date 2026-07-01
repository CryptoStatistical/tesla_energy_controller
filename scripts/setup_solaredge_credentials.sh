#!/usr/bin/env bash
set -euo pipefail

directory="${1:-.secrets}"
mkdir -p "$directory"
umask 077

read -r -p "Email SolarEdge: " username
read -r -s -p "Password SolarEdge (non verrà mostrata): " password
echo

if [[ -z "$username" || -z "$password" ]]; then
  echo "Email e password non possono essere vuote." >&2
  exit 1
fi

printf '%s\n' "$username" > "$directory/solaredge_username"
printf '%s\n' "$password" > "$directory/solaredge_password"
chmod 600 "$directory/solaredge_username" "$directory/solaredge_password"
unset password
echo "Credenziali salvate localmente in $directory (permessi 0600)."
