#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-apply}"

NETWORK_DEVICE="${NETWORK_DEVICE:-wlan0}"

STOREDGE_ROUTE_ENABLED="${STOREDGE_ROUTE_ENABLED:-true}"
STOREDGE_ROUTE_CIDR="${STOREDGE_ROUTE_CIDR:-192.168.2.0/24}"
STOREDGE_ROUTE_GATEWAY="${STOREDGE_ROUTE_GATEWAY:-192.168.1.61}"

ALFA_NEIGHBOR_ENABLED="${ALFA_NEIGHBOR_ENABLED:-true}"
ALFA_NEIGHBOR_IP="${ALFA_NEIGHBOR_IP:-192.168.1.169}"
ALFA_NEIGHBOR_MAC="${ALFA_NEIGHBOR_MAC:-34:ab:95:5a:be:68}"
ALFA_MODBUS_PORT="${ALFA_MODBUS_PORT:-502}"

log() {
  printf '%s\n' "$*"
}

enabled() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

wait_for_device() {
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if ip link show "$NETWORK_DEVICE" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  log "Device $NETWORK_DEVICE non disponibile"
  return 1
}

apply_network() {
  wait_for_device

  if enabled "$STOREDGE_ROUTE_ENABLED"; then
    log "Installo rotta StorEdge: $STOREDGE_ROUTE_CIDR via $STOREDGE_ROUTE_GATEWAY dev $NETWORK_DEVICE"
    ip route replace "$STOREDGE_ROUTE_CIDR" via "$STOREDGE_ROUTE_GATEWAY" dev "$NETWORK_DEVICE"
  fi

  if enabled "$ALFA_NEIGHBOR_ENABLED"; then
    log "Installo neighbor ALFA: $ALFA_NEIGHBOR_IP -> $ALFA_NEIGHBOR_MAC dev $NETWORK_DEVICE"
    ip neigh replace "$ALFA_NEIGHBOR_IP" lladdr "$ALFA_NEIGHBOR_MAC" dev "$NETWORK_DEVICE" nud permanent
  fi
}

remove_network() {
  if enabled "$STOREDGE_ROUTE_ENABLED"; then
    ip route del "$STOREDGE_ROUTE_CIDR" via "$STOREDGE_ROUTE_GATEWAY" dev "$NETWORK_DEVICE" 2>/dev/null || true
  fi

  if enabled "$ALFA_NEIGHBOR_ENABLED"; then
    ip neigh del "$ALFA_NEIGHBOR_IP" dev "$NETWORK_DEVICE" 2>/dev/null || true
  fi
}

status_network() {
  log "Route StorEdge:"
  ip route show "$STOREDGE_ROUTE_CIDR" || true
  log "Neighbor ALFA:"
  ip neigh show "$ALFA_NEIGHBOR_IP" || true
  if command -v nc >/dev/null 2>&1; then
    nc -vz -w 3 "$ALFA_NEIGHBOR_IP" "$ALFA_MODBUS_PORT" || true
  fi
}

case "$ACTION" in
  apply)
    apply_network
    status_network
    ;;
  remove)
    remove_network
    ;;
  status)
    status_network
    ;;
  *)
    log "Uso: $0 [apply|remove|status]"
    exit 2
    ;;
esac
