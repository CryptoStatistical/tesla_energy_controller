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

temperature_conf=/etc/rpimonitor/template/temperature.conf
if [[ -f "$temperature_conf" ]]; then
  echo "==> Correzione lettura temperatura CPU"
  # RPi-Monitor 2.13 can display null when the temperature template uses sprintf().
  sed -i -E 's|^dynamic[.]1[.]postprocess=.*|dynamic.1.postprocess=$1/1000|' "$temperature_conf"
  if [[ -e /sys/class/thermal/thermal_zone0/temp ]]; then
    sed -i -E 's|^dynamic[.]1[.]source=.*|dynamic.1.source=/sys/class/thermal/thermal_zone0/temp|' "$temperature_conf"
  fi
fi

data_conf=/etc/rpimonitor/data.conf
if [[ -f "$data_conf" ]] && ! grep -Eq '^[[:space:]]*include=/etc/rpimonitor/template/temperature[.]conf' "$data_conf"; then
  printf '\n%s\n' 'include=/etc/rpimonitor/template/temperature.conf' >>"$data_conf"
fi

cpu_usage_helper=/usr/local/bin/rpimonitor-cpu-usage
cat >"$cpu_usage_helper" <<'CPU_USAGE_SCRIPT'
#!/bin/sh
set -eu

state=/var/lib/rpimonitor/cpu_usage.state

read_cpu() {
  awk '/^cpu / {
    idle=$5+$6
    total=0
    for (i=2; i<=8; i++) total += $i
    print total, idle
    exit
  }' /proc/stat
}

set -- $(read_cpu)
total=$1
idle=$2

if [ -r "$state" ]; then
  set -- $(cat "$state")
  prev_total=$1
  prev_idle=$2
else
  prev_total=$total
  prev_idle=$idle
  sleep 0.2
  set -- $(read_cpu)
  total=$1
  idle=$2
fi

printf '%s %s\n' "$total" "$idle" >"$state"

delta_total=$((total - prev_total))
delta_idle=$((idle - prev_idle))

awk -v total="$delta_total" -v idle="$delta_idle" 'BEGIN {
  if (total <= 0) {
    print "0.00"
    exit
  }
  usage = (total - idle) * 100 / total
  if (usage < 0) usage = 0
  if (usage > 100) usage = 100
  printf "%.2f\n", usage
}'
CPU_USAGE_SCRIPT
chmod 0755 "$cpu_usage_helper"

cpu_conf=/etc/rpimonitor/template/cpu.conf
if [[ -f "$cpu_conf" ]]; then
  echo "==> Configurazione grafico CPU usage"
  if ! grep -Eq '^dynamic[.][0-9]+[.]name=cpu_usage_percent$' "$cpu_conf"; then
    tmp="$(mktemp)"
    awk '
      BEGIN { inserted=0 }
      /^static[.]1[.]name=/ && !inserted {
        print ""
        print "dynamic.5.name=cpu_usage_percent"
        print "dynamic.5.source=/usr/local/bin/rpimonitor-cpu-usage"
        print "dynamic.5.regexp=([0-9.]+)"
        print "dynamic.5.postprocess="
        print "dynamic.5.rrd=GAUGE"
        inserted=1
      }
      { print }
      END {
        if (!inserted) {
          print ""
          print "dynamic.5.name=cpu_usage_percent"
          print "dynamic.5.source=/usr/local/bin/rpimonitor-cpu-usage"
          print "dynamic.5.regexp=([0-9.]+)"
          print "dynamic.5.postprocess="
          print "dynamic.5.rrd=GAUGE"
        }
      }
    ' "$cpu_conf" >"$tmp"
    cat "$tmp" >"$cpu_conf"
    rm -f "$tmp"
  fi

  sed -i -E \
    -e 's#^web[.]status[.]1[.]content[.]1[.]line[.]1=.*#web.status.1.content.1.line.1=JustGageBar("Usage", data.cpu_usage_percent+"%", 0, data.cpu_usage_percent, 100, 75, 90)+" Load: <b>"+data.load1+"</b> [1min] - <b>"+data.load5+"</b> [5min] - <b>"+data.load15+"</b> [15min]"#' \
    -e 's#^web[.]statistics[.]1[.]content[.]1[.]title=.*#web.statistics.1.content.1.title="CPU Usage"#' \
    -e 's#^web[.]statistics[.]1[.]content[.]1[.]graph[.]1=.*#web.statistics.1.content.1.graph.1=cpu_usage_percent#' \
    -e 's#^(web[.]statistics[.]1[.]content[.]1[.]graph[.][23]=)#\#\1#' \
    "$cpu_conf"

  if ! grep -Eq '^web[.]statistics[.]1[.]content[.]1[.]ds_graph_options[.]cpu_usage_percent[.]label=' "$cpu_conf"; then
    printf '%s\n' \
      'web.statistics.1.content.1.ds_graph_options.cpu_usage_percent.label=CPU usage (%)' \
      'web.statistics.1.content.1.ds_graph_options.cpu_usage_percent.lines={ fill: true }' \
      >>"$cpu_conf"
  fi
  if ! grep -Eq '^web[.]statistics[.]1[.]content[.]1[.]graph_options[.]yaxis=' "$cpu_conf"; then
    printf '%s\n' 'web.statistics.1.content.1.graph_options.yaxis={ min: 0, max: 100, tickFormatter: function (v) { return v + "%" } }' >>"$cpu_conf"
  fi
fi

network_rate_helper=/usr/local/bin/rpimonitor-network-rate
cat >"$network_rate_helper" <<'NETWORK_RATE_SCRIPT'
#!/bin/sh
set -eu

iface="${1:-}"
if [ -z "$iface" ]; then
  iface="$(ip route show default 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')"
fi
if [ -z "$iface" ]; then
  iface=wlan0
fi

rx_file="/sys/class/net/$iface/statistics/rx_bytes"
tx_file="/sys/class/net/$iface/statistics/tx_bytes"
state="/var/lib/rpimonitor/${iface}_network_rate.state"

read_counters() {
  now="$(date +%s%N)"
  rx="$(cat "$rx_file")"
  tx="$(cat "$tx_file")"
  printf '%s %s %s\n' "$now" "$rx" "$tx"
}

set -- $(read_counters)
now=$1
rx=$2
tx=$3

if [ -r "$state" ]; then
  set -- $(cat "$state")
  prev_now=$1
  prev_rx=$2
  prev_tx=$3
else
  prev_now=$now
  prev_rx=$rx
  prev_tx=$tx
  sleep 0.5
  set -- $(read_counters)
  now=$1
  rx=$2
  tx=$3
fi

printf '%s %s %s\n' "$now" "$rx" "$tx" >"$state"

awk \
  -v dt_ns="$((now - prev_now))" \
  -v rx_delta="$((rx - prev_rx))" \
  -v tx_delta="$((tx - prev_tx))" \
  'BEGIN {
    if (dt_ns <= 0) {
      print "0.00 0.00"
      exit
    }
    if (rx_delta < 0) rx_delta = 0
    if (tx_delta < 0) tx_delta = 0
    seconds = dt_ns / 1000000000
    printf "%.2f %.2f\n", rx_delta / seconds, tx_delta / seconds
  }'
NETWORK_RATE_SCRIPT
chmod 0755 "$network_rate_helper"

network_conf=/etc/rpimonitor/template/network.conf
if [[ -f "$network_conf" ]]; then
  network_interface="$(ip route show default 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')"
  if [[ -z "$network_interface" ]]; then
    for candidate in wlan0 eth0; do
      if [[ -d "/sys/class/net/$candidate" ]]; then
        network_interface="$candidate"
        break
      fi
    done
  fi

  if [[ -n "$network_interface" ]]; then
    echo "==> Configurazione rete RPi-Monitor su $network_interface"
    sed -i -E \
      -e "s#/sys/class/net/[^/]+/statistics/rx_bytes#/sys/class/net/${network_interface}/statistics/rx_bytes#g" \
      -e "s#/sys/class/net/[^/]+/statistics/tx_bytes#/sys/class/net/${network_interface}/statistics/tx_bytes#g" \
      -e 's#content[.]1[.]7ds_graph_options#content.1.ds_graph_options#g' \
      -e 's#Upload bandwidth [(]bytes[)]#Upload bandwidth (B/s)#' \
      -e 's#Download bandwidth [(]bytes[)]#Download bandwidth (B/s)#' \
      "$network_conf"

    if grep -Eq '^dynamic[.][0-9]+[.]name=wifi_rx_rate,wifi_tx_rate$' "$network_conf"; then
      sed -i -E "s#^dynamic[.][0-9]+[.]source=/usr/local/bin/rpimonitor-network-rate.*#dynamic.3.source=/usr/local/bin/rpimonitor-network-rate ${network_interface}#" "$network_conf"
    else
      printf '\n%s\n%s\n%s\n%s\n%s\n' \
        'dynamic.3.name=wifi_rx_rate,wifi_tx_rate' \
        "dynamic.3.source=/usr/local/bin/rpimonitor-network-rate ${network_interface}" \
        'dynamic.3.regexp=^([0-9.]+) ([0-9.]+)' \
        'dynamic.3.postprocess=' \
        'dynamic.3.rrd=GAUGE' \
        >>"$network_conf"
    fi

    sed -i -E 's#^web[.]status[.]1[.]content[.]1[.]line[.]1=.*#web.status.1.content.1.line.1="Istantaneo: <b>"+KMG(data.wifi_tx_rate)+"/s <i class='"'"'icon-arrow-up'"'"'></i></b> <b>"+KMG(data.wifi_rx_rate)+"/s <i class='"'"'icon-arrow-down'"'"'></i></b>"#' "$network_conf"
    if grep -Eq '^web[.]status[.]1[.]content[.]1[.]line[.]2=' "$network_conf"; then
      sed -i -E 's#^web[.]status[.]1[.]content[.]1[.]line[.]2=.*#web.status.1.content.1.line.2="Total sent: <b>"+KMG(data.net_send)+"<i class='"'"'icon-arrow-up'"'"'></i></b> Received: <b>"+KMG(Math.abs(data.net_received))+"<i class='"'"'icon-arrow-down'"'"'></i></b>"#' "$network_conf"
    else
      sed -i -E '/^web[.]status[.]1[.]content[.]1[.]line[.]1=/a web.status.1.content.1.line.2="Total sent: <b>"+KMG(data.net_send)+"<i class='"'"'icon-arrow-up'"'"'></i></b> Received: <b>"+KMG(Math.abs(data.net_received))+"<i class='"'"'icon-arrow-down'"'"'></i></b>"' "$network_conf"
    fi

    if [[ "$network_interface" =~ ^wl ]]; then
      sed -i -E \
        -e 's#Ethernet#Wi-Fi#g' \
        -e 's#^web[.]status[.]1[.]content[.]1[.]title=.*#web.status.1.content.1.title="Wi-Fi"#' \
        -e 's#^web[.]statistics[.]1[.]content[.]1[.]title=.*#web.statistics.1.content.1.title="Wi-Fi"#' \
        "$network_conf"
    fi
  fi
fi

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
