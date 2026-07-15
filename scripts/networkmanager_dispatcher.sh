#!/usr/bin/env bash
set -euo pipefail

NETWORK_CONFIG_FILE="${NETWORK_CONFIG_FILE:-/etc/default/tesla-energy-controller-network}"
if [[ -r "$NETWORK_CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$NETWORK_CONFIG_FILE"
fi

NETWORK_DEVICE="${NETWORK_DEVICE:-wlan0}"
NETWORK_BOOTSTRAP="${NETWORK_BOOTSTRAP:-/usr/local/sbin/tesla-energy-controller-network}"
interface="${1:-}"
action="${2:-}"

if [[ "$interface" != "$NETWORK_DEVICE" ]]; then
  exit 0
fi

case "$action" in
  up|dhcp4-change)
    logger -t tesla-energy-network -- \
      "$NETWORK_DEVICE attiva ($action): ripristino regole StorEdge/ALFA"
    "$NETWORK_BOOTSTRAP" restore
    ;;
esac
