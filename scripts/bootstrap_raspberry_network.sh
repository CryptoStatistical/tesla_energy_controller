#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-apply}"

NETWORK_DEVICE="${NETWORK_DEVICE:-wlan0}"

WIFI_RESILIENCE_ENABLED="${WIFI_RESILIENCE_ENABLED:-true}"
WIFI_CONNECTION_NAME="${WIFI_CONNECTION_NAME:-}"

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
  case "$1" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
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

active_wifi_connection() {
  local connection

  if [[ -n "$WIFI_CONNECTION_NAME" ]]; then
    printf '%s\n' "$WIFI_CONNECTION_NAME"
    return 0
  fi

  if ! command -v nmcli >/dev/null 2>&1; then
    return 1
  fi

  connection="$(nmcli -g GENERAL.CONNECTION device show "$NETWORK_DEVICE" 2>/dev/null | head -n 1)"
  if [[ -n "$connection" && "$connection" != "--" ]]; then
    printf '%s\n' "$connection"
    return 0
  fi
  return 1
}

apply_wifi_resilience() {
  local connection=""

  if ! enabled "$WIFI_RESILIENCE_ENABLED"; then
    return 0
  fi

  if command -v nmcli >/dev/null 2>&1; then
    connection="$(active_wifi_connection || true)"
    if [[ -n "$connection" ]]; then
      log "Configuro riconnessione Wi-Fi persistente sul profilo: $connection"
      nmcli connection modify "$connection" \
        connection.autoconnect yes \
        connection.autoconnect-retries 0 \
        802-11-wireless.powersave 2
    else
      log "Profilo NetworkManager di $NETWORK_DEVICE non individuato"
    fi
  fi

  if command -v iw >/dev/null 2>&1; then
    log "Disabilito il power saving Wi-Fi su $NETWORK_DEVICE"
    iw dev "$NETWORK_DEVICE" set power_save off
  fi
}

apply_network_rules() {
  if enabled "$STOREDGE_ROUTE_ENABLED"; then
    log "Installo rotta StorEdge: $STOREDGE_ROUTE_CIDR via $STOREDGE_ROUTE_GATEWAY dev $NETWORK_DEVICE"
    ip route replace "$STOREDGE_ROUTE_CIDR" via "$STOREDGE_ROUTE_GATEWAY" dev "$NETWORK_DEVICE"
  fi

  if enabled "$ALFA_NEIGHBOR_ENABLED"; then
    log "Installo neighbor ALFA: $ALFA_NEIGHBOR_IP -> $ALFA_NEIGHBOR_MAC dev $NETWORK_DEVICE"
    ip neigh replace "$ALFA_NEIGHBOR_IP" lladdr "$ALFA_NEIGHBOR_MAC" dev "$NETWORK_DEVICE" nud permanent
  fi
}

apply_network() {
  wait_for_device
  apply_wifi_resilience
  apply_network_rules
}

restore_network_rules() {
  wait_for_device
  apply_network_rules
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
  log "Wi-Fi:"
  if command -v nmcli >/dev/null 2>&1; then
    connection="$(active_wifi_connection || true)"
    if [[ -n "$connection" ]]; then
      nmcli -g connection.autoconnect,connection.autoconnect-retries,802-11-wireless.powersave \
        connection show "$connection" || true
    fi
  fi
  if command -v iw >/dev/null 2>&1; then
    iw dev "$NETWORK_DEVICE" get power_save || true
  fi
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
  restore)
    restore_network_rules
    status_network
    ;;
  *)
    log "Uso: $0 [apply|remove|restore|status]"
    exit 2
    ;;
esac
