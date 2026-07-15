#!/usr/bin/env bash
set -euo pipefail

NETWORK_DEVICE="${NETWORK_DEVICE:-wlan0}"
WIFI_RESILIENCE_ENABLED="${WIFI_RESILIENCE_ENABLED:-true}"
WIFI_CONNECTION_NAME="${WIFI_CONNECTION_NAME:-}"
WIFI_GATEWAY="${WIFI_GATEWAY:-192.168.1.1}"
WIFI_FAILURE_THRESHOLD="${WIFI_FAILURE_THRESHOLD:-3}"
WIFI_PING_COUNT="${WIFI_PING_COUNT:-2}"
WIFI_PING_TIMEOUT_SECONDS="${WIFI_PING_TIMEOUT_SECONDS:-2}"
WIFI_RECOVERY_WAIT_SECONDS="${WIFI_RECOVERY_WAIT_SECONDS:-20}"
STATE_FILE="${WIFI_WATCHDOG_STATE_FILE:-/run/tesla-energy-controller-network-watchdog.failures}"
LOCK_FILE="${WIFI_WATCHDOG_LOCK_FILE:-/run/tesla-energy-controller-network-watchdog.lock}"
NETWORK_BOOTSTRAP="${NETWORK_BOOTSTRAP:-/usr/local/sbin/tesla-energy-controller-network}"

enabled() {
  case "$1" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

log() {
  logger -t tesla-energy-network-watchdog -- "$*"
  printf '%s\n' "$*"
}

active_connection() {
  local connection

  if [[ -n "$WIFI_CONNECTION_NAME" ]]; then
    printf '%s\n' "$WIFI_CONNECTION_NAME"
    return 0
  fi

  connection="$(nmcli -g GENERAL.CONNECTION device show "$NETWORK_DEVICE" 2>/dev/null | head -n 1)"
  if [[ -n "$connection" && "$connection" != "--" ]]; then
    printf '%s\n' "$connection"
    return 0
  fi

  connection="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null \
    | awk -F: '$2 == "802-11-wireless" { print $1; exit }')"
  [[ -n "$connection" ]] || return 1
  printf '%s\n' "$connection"
}

gateway_reachable() {
  ping -I "$NETWORK_DEVICE" -c "$WIFI_PING_COUNT" -W "$WIFI_PING_TIMEOUT_SECONDS" \
    "$WIFI_GATEWAY" >/dev/null 2>&1
}

recover_connection() {
  local connection=""

  log "Gateway $WIFI_GATEWAY irraggiungibile: avvio recupero di $NETWORK_DEVICE"
  if command -v nmcli >/dev/null 2>&1; then
    connection="$(active_connection || true)"
    nmcli device disconnect "$NETWORK_DEVICE" >/dev/null 2>&1 || true
    sleep 2
    if [[ -n "$connection" ]]; then
      if ! nmcli connection up "$connection" ifname "$NETWORK_DEVICE" >/dev/null; then
        log "Riattivazione del profilo $connection fallita; provo il collegamento automatico"
        nmcli device connect "$NETWORK_DEVICE" >/dev/null
      fi
    else
      nmcli device connect "$NETWORK_DEVICE" >/dev/null
    fi
  else
    ip link set "$NETWORK_DEVICE" down
    sleep 2
    ip link set "$NETWORK_DEVICE" up
  fi

  for ((elapsed = 0; elapsed < WIFI_RECOVERY_WAIT_SECONDS; elapsed += 2)); do
    sleep 2
    if gateway_reachable; then
      "$NETWORK_BOOTSTRAP" apply
      log "Connessione Wi-Fi recuperata; rotta StorEdge e neighbor ALFA ripristinati"
      return 0
    fi
  done

  log "Recupero Wi-Fi non riuscito; nuovo tentativo al prossimo ciclo"
  return 1
}

if ! enabled "$WIFI_RESILIENCE_ENABLED"; then
  exit 0
fi

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

if gateway_reachable; then
  printf '0\n' >"$STATE_FILE"
  exit 0
fi

failures="$(cat "$STATE_FILE" 2>/dev/null || printf '0')"
[[ "$failures" =~ ^[0-9]+$ ]] || failures=0
failures=$((failures + 1))
printf '%s\n' "$failures" >"$STATE_FILE"
log "Verifica Wi-Fi fallita ($failures/$WIFI_FAILURE_THRESHOLD) verso $WIFI_GATEWAY"

if ((failures < WIFI_FAILURE_THRESHOLD)); then
  exit 0
fi

if recover_connection; then
  printf '0\n' >"$STATE_FILE"
fi
