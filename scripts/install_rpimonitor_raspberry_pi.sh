#!/usr/bin/env bash
set -euo pipefail

remote="${1:-rasp26@192.168.1.200}"
rpimonitor_port="${2:-8888}"
energy_port="${ENERGY_WEB_PORT:-8080}"

if [[ "$rpimonitor_port" == "$energy_port" ]]; then
  echo "Errore: RPi-Monitor non deve usare la stessa porta della dashboard energy ($energy_port)." >&2
  exit 1
fi

if ! [[ "$rpimonitor_port" =~ ^[0-9]+$ ]] || ((rpimonitor_port < 1 || rpimonitor_port > 65535)); then
  echo "Errore: porta RPi-Monitor non valida: $rpimonitor_port" >&2
  exit 1
fi

if ! [[ "$energy_port" =~ ^[0-9]+$ ]] || ((energy_port < 1 || energy_port > 65535)); then
  echo "Errore: porta dashboard energy non valida: $energy_port" >&2
  exit 1
fi

echo "==> Target Raspberry: $remote"
echo "==> Dashboard energy attesa su :$energy_port"
echo "==> RPi-Monitor su :$rpimonitor_port"

ssh -tt "$remote" "RPIMONITOR_PORT='$rpimonitor_port' ENERGY_PORT='$energy_port' sudo -E bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

port_listeners() {
  ss -H -tlnp 2>/dev/null | awk -v port=":$1" '$4 ~ port "$" {print}'
}

energy_health_before=unknown
energy_service_before=inactive
energy_port_before=closed

if systemctl is-active --quiet tesla-energy-controller.service; then
  energy_service_before=active
fi

if [[ -n "$(port_listeners "$ENERGY_PORT")" ]]; then
  energy_port_before=open
fi

if curl -fsS --max-time 5 "http://127.0.0.1:${ENERGY_PORT}/health" >/dev/null 2>&1; then
  energy_health_before=ok
fi

echo "==> Stato energy prima: service=$energy_service_before port=$energy_port_before health=$energy_health_before"

existing_rpimonitor_listener="$(port_listeners "$RPIMONITOR_PORT" || true)"
if [[ -n "$existing_rpimonitor_listener" ]] && ! grep -qiE 'rpimonitor|perl' <<<"$existing_rpimonitor_listener"; then
  echo "Errore: la porta $RPIMONITOR_PORT e' gia' occupata da:" >&2
  echo "$existing_rpimonitor_listener" >&2
  echo "Rilancia lo script scegliendo una porta libera, ad esempio: scripts/install_rpimonitor_raspberry_pi.sh <host> 8889" >&2
  exit 1
fi

echo "==> Installazione RPi-Monitor da pacchetto .deb ufficiale"
rm -f /etc/apt/sources.list.d/rpimonitor.list /etc/apt/keyrings/rpimonitor.gpg
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl

deb_file=/tmp/rpimonitor_latest.deb
curl -fsSL \
  https://raw.githubusercontent.com/XavierBerger/RPi-Monitor-deb/develop/packages/rpimonitor_latest.deb \
  -o "$deb_file"
DEBIAN_FRONTEND=noninteractive apt-get install -y "$deb_file"

daemon_conf=/etc/rpimonitor/daemon.conf
if [[ -f "$daemon_conf" ]]; then
  if grep -Eq '^[#[:space:]]*daemon[.]port=' "$daemon_conf"; then
    sed -i -E "s/^[#[:space:]]*daemon[.]port=.*/daemon.port=${RPIMONITOR_PORT}/" "$daemon_conf"
  else
    printf '\n%s\n' "daemon.port=${RPIMONITOR_PORT}" >>"$daemon_conf"
  fi
fi

if [[ -x /etc/init.d/rpimonitor ]]; then
  /etc/init.d/rpimonitor update || true
elif [[ -x /usr/share/rpimonitor/scripts/updatePackagesStatus.pl ]]; then
  /usr/share/rpimonitor/scripts/updatePackagesStatus.pl || true
fi

systemctl daemon-reload
systemctl enable rpimonitor >/dev/null 2>&1 || true
systemctl restart rpimonitor

echo "==> Verifica RPi-Monitor"
sleep 2
if ! curl -fsS --max-time 10 "http://127.0.0.1:${RPIMONITOR_PORT}/" >/dev/null; then
  echo "Errore: RPi-Monitor installato ma non risponde su http://127.0.0.1:${RPIMONITOR_PORT}/" >&2
  systemctl --no-pager --lines=40 status rpimonitor >&2 || true
  exit 1
fi

echo "==> Verifica dashboard energy dopo installazione"
if [[ "$energy_service_before" == active ]] && ! systemctl is-active --quiet tesla-energy-controller.service; then
  echo "Errore: tesla-energy-controller era attivo prima dell'installazione e ora non lo e'." >&2
  systemctl --no-pager --lines=40 status tesla-energy-controller.service >&2 || true
  exit 1
fi

if [[ "$energy_port_before" == open ]] && [[ -z "$(port_listeners "$ENERGY_PORT")" ]]; then
  echo "Errore: la porta energy $ENERGY_PORT era aperta prima dell'installazione e ora non lo e'." >&2
  exit 1
fi

if [[ "$energy_health_before" == ok ]]; then
  curl -fsS --max-time 5 "http://127.0.0.1:${ENERGY_PORT}/health" >/dev/null
fi

echo "==> Porte in ascolto rilevanti"
port_listeners "$ENERGY_PORT" || true
port_listeners "$RPIMONITOR_PORT" || true

echo "OK: RPi-Monitor e' disponibile su http://$(hostname -I | awk '{print $1}'):${RPIMONITOR_PORT}/"
REMOTE_SCRIPT
