#!/bin/sh
# Ada 22-Oct-2026 collect Delilah (GL-X3000) WAN / failover / cell stats for the
# /van page. Runs ON DELILAH over ssh; writes JSON to stdout, nothing else.
# Ada 01-Nov-2026 failover: poll all kmwan members (including secondwan and
# modem_0001), ordered by metric; emit kmwan_members JSON array for the page.
# Deliberately dependency-free: sh, uci, ubus, awk, iwinfo, /proc only.
# Modem is DISABLED on this build (uci modem_0001.disabled=1), so cell fields
# come out null rather than absent -- the page renders "modem disabled".
set -u

# --- system -----------------------------------------------------------------
SYS=$(ubus call system info 2>/dev/null)
UPTIME=$(echo "$SYS" | sed -n 's/.*"uptime": \([0-9]*\).*/\1/p' | head -1)
LOAD=$(echo "$SYS" | tr -d '\n\t ' | sed -n 's/.*"load":\[\([0-9,]*\)\].*/\1/p' | head -1)
# Ada 24-Oct-2026 OpenWrt reports the load average as FIXED POINT with a 65536
# scale: the wiki says "to get to the familiar values reported by uptime divide
# these numbers by 65536.0". The page was showing the raw integers, which read as
# nonsense -- 57696,25344,12544 is really 0.88 / 0.39 / 0.19, and 1376,2976,3968
# is 0.02 / 0.05 / 0.06 (both cross-checked against /proc/loadavg on the router).
# Emit the familiar 1 / 5 / 15 minute averages instead.
if [ -n "$LOAD" ]; then
  LOAD=$(echo "$LOAD" | awk -F, 'NF==3{printf "%.2f,%.2f,%.2f", $1/65536, $2/65536, $3/65536}')
fi
MEM_TOTAL=$(echo "$SYS" | tr -d '\n\t ' | sed -n 's/.*"total":\([0-9]*\).*/\1/p' | head -1)
MEM_FREE=$(echo "$SYS" | tr -d '\n\t ' | sed -n 's/.*"free":\([0-9]*\).*/\1/p' | head -1)
MEM_AVAIL=$(echo "$SYS" | tr -d '\n\t ' | sed -n 's/.*"available":\([0-9]*\).*/\1/p' | head -1)
OVERLAY_PCT=$(df /overlay 2>/dev/null | awk 'NR==2{gsub("%","",$5);print $5}')
TEMP=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
[ -n "$TEMP" ] && TEMP=$((TEMP / 1000))

# --- WAN interface (Wi-Fi client to the Mini's router) -----------------------
WAN=$(ubus call network.interface.wwan status 2>/dev/null)
WAN_UPTIME=$(echo "$WAN" | sed -n 's/.*"uptime": \([0-9]*\).*/\1/p' | head -1)
WAN_ADDR=$(echo "$WAN" | sed -n 's/.*"address": "\([0-9.]*\)".*/\1/p' | head -1)
WAN_GW=$(echo "$WAN" | sed -n 's/.*"nexthop": "\([0-9.]*\)".*/\1/p' | head -1)
WAN_DNS=$(echo "$WAN" | sed -n 's/.*"dns-server":\[\s*"\([0-9.]*\)"/\1/p' | head -1)
WAN_METRIC=$(echo "$WAN" | sed -n 's/.*"metric": \([0-9]*\).*/\1/p' | head -1)
WAN_DEV=$(echo "$WAN" | sed -n 's/.*"device": "\([^"]*\)".*/\1/p' | head -1)
[ -z "$WAN_DEV" ] && WAN_DEV=apclix0
RX=$(cat /sys/class/net/$WAN_DEV/statistics/rx_bytes 2>/dev/null)
TX=$(cat /sys/class/net/$WAN_DEV/statistics/tx_bytes 2>/dev/null)

# --- wifi link --------------------------------------------------------------
WIFI=$(iwinfo "$WAN_DEV" info 2>/dev/null)
SSID=$(echo "$WIFI" | sed -n 's/.*ESSID: "\([^"]*\)".*/\1/p')
AP=$(echo "$WIFI" | sed -n 's/.*Access Point: \([0-9A-Fa-f:]*\).*/\1/p')
CHANNEL=$(echo "$WIFI" | sed -n 's/.*Channel: \([0-9]*\).*/\1/p')
FREQ=$(echo "$WIFI" | sed -n 's/.*Channel: [0-9]* (\([0-9.]*\) GHz.*/\1/p')
RATE=$(echo "$WIFI" | sed -n 's/.*Bit Rate: \([0-9.]*\).*/\1/p')
SIGNAL=$(echo "$WIFI" | sed -n 's/.*Signal: \(-*[0-9]*\) dBm.*/\1/p')
NOISE=$(echo "$WIFI" | sed -n 's/.*Noise: \(-*[0-9]*\) dBm.*/\1/p')
QUALITY=$(echo "$WIFI" | sed -n 's/.*Link Quality: \([0-9]*\).*/\1/p')
QUALITY_MAX=$(echo "$WIFI" | sed -n 's/.*Link Quality: [0-9]*\/\([0-9]*\).*/\1/p')

# ---- cell modem: AT over the modem's own tty, bounded by a wall-clock deadline
# The ubus objects this firmware names (mvas, modem) are NOT registered on this
# build, and /usr/bin/modem_AT only answers AT+CIMI, so drive the port directly.
# GL.iNet's own modem_manager holds the same ttyUSB port and interleaves ERROR
# replies, so each command is retried and accepted only when it carries its own
# "+TAG:" marker. The DEADLINE means the router/WAN half of the page always
# returns on time even when the modem is silent.
DEADLINE=$(( $(date +%s) + 25 ))
past_deadline() { [ "$(date +%s)" -ge "$DEADLINE" ]; }

# at_read <port> <command> [expected_tag] -- open, write, read, close per call
at_read() {
  _port=$1; _cmd=$2; _tag=${3:-}
  _i=0
  while [ "$_i" -lt 3 ]; do
    past_deadline && return 1
    {
      eval "exec 3<>\"\$_port\"" 2>/dev/null || return 1
      printf 'AT+%s\r' "$_cmd" >&3 2>/dev/null
      timeout 1 cat <&3 2>/dev/null | tr -d '\r'
      eval "exec 3<&-" 2>/dev/null
    } > /tmp/.at_out.$$ 2>/dev/null
    if [ -n "$_tag" ]; then
      grep -q "+$_tag:" /tmp/.at_out.$$ 2>/dev/null && { cat /tmp/.at_out.$$; rm -f /tmp/.at_out.$$; return 0; }
    else
      grep -q "OK" /tmp/.at_out.$$ 2>/dev/null && { cat /tmp/.at_out.$$; rm -f /tmp/.at_out.$$; return 0; }
    fi
    _i=$((_i + 1))
    sleep 0.2
  done
  cat /tmp/.at_out.$$ 2>/dev/null; rm -f /tmp/.at_out.$$
  return 1
}

ATPORT=""
for p in /dev/ttyUSB2 /dev/ttyUSB1 /dev/ttyUSB0 /dev/ttyUSB3; do
  [ -c "$p" ] && ATPORT="$p" && break
done

CSQ=""; QCSQ=""; QNWINFO=""; QENG=""; QCAINFO=""; COPS=""
CCLK=""; CIMI=""; FWVER=""; CEREG=""

# Ada 24-Oct-2026 the gate was a SINGLE CSQ read: when the modem did not answer
# it, CELL_OK stayed 0 and every cell field was emitted empty, so the whole modem
# box went blank (the poll log shows csq= empty on roughly one poll in four).
# Retry the gate before giving up. The DEADLINE above was raised 18s -> 25s to
# pay for it; poll.py allows 45s for the whole ssh call.
CELL_OK=0
if [ -n "$ATPORT" ]; then
  _g=0
  while [ "$_g" -lt 3 ] && [ "$CELL_OK" = "0" ]; do
    past_deadline && break
    at_read "$ATPORT" "CSQ" "CSQ" >/dev/null 2>&1 && CELL_OK=1
    _g=$((_g + 1))
    [ "$CELL_OK" = "0" ] && sleep 1
  done
fi
if [ "$CELL_OK" = "1" ]; then
  CELL_OK=1
  CSQ=$(     at_read "$ATPORT" "CSQ"     "CSQ"     | sed -n 's/^+CSQ: //p'     | head -1)
  QCSQ=$(    at_read "$ATPORT" "QCSQ"    "QCSQ"    | sed -n 's/^+QCSQ: //p'    | head -1)
  QNWINFO=$( at_read "$ATPORT" "QNWINFO" "QNWINFO" | sed -n 's/^+QNWINFO: //p' | head -1)
  QENG=$(    at_read "$ATPORT" 'QENG="servingcell"' "QENG" | sed -n 's/^+QENG: //p' | head -1)
  QCAINFO=$( at_read "$ATPORT" "QCAINFO" "QCAINFO" | sed -n 's/^+QCAINFO: //p' | head -1)
  COPS=$(    at_read "$ATPORT" "COPS?"   "COPS"    | sed -n 's/^+COPS: //p'    | head -1)
  CCLK=$(    at_read "$ATPORT" "CCLK?"   "CCLK"    | sed -n 's/^+CCLK: //p'    | head -1)
  CIMI=$(    at_read "$ATPORT" "CIMI"    "CIMI"    | sed -n 's/^+CIMI: //p'    | head -1)
  CEREG=$(   at_read "$ATPORT" "CEREG?"  "CEREG"   | sed -n 's/^+CEREG: //p'   | head -1)
  # Ada 24-Oct-2026 the modem answers AT+GMR with an UNTAGGED line -- there is no
  # "+QGMR:" prefix, so grepping for one never matched and fw_version was always
  # empty (the page showed a dash). AT+QGMR carries the same string plus a
  # bootloader suffix, but at 31 characters it will not fit a 300px box, so this
  # reads the shorter AT+GMR reply instead: RM520NGLAAR01A07M4G.
  FWVER=$(at_read "$ATPORT" "GMR" "" | tr -d '\r' | sed '/^$/d' | grep -v '^OK$' | head -1)
fi
# The modem replies keep their own embedded quotes; q() escapes them for JSON,
# and the page strips them when it parses a field. Nothing stripped here.

# --- failover chain (kmwan, read from uci -- no ubus object exists) ----------
KM_MODE=$(uci -q get kmwan.global.mode)
KM_LEVEL=$(uci -q get kmwan.global.level)
KM_SENS=$(uci -q get kmwan.global.sensitivity)

# Ada 01-Nov-2026 read all enabled kmwan members sorted by metric (lowest =
# highest priority), including secondwan and modem_0001. The page shows the top
# three from this ordered list.
_TMP=/tmp/kmwan_list.$$
uci show kmwan | grep '=member$' | cut -d= -f1 | while read -r section; do
  interface=$(uci -q get "$section.interface")
  metric=$(uci -q get "$section.metric")
  disabled=$(uci -q get "$section.disabled")
  [ -z "$interface" ] && continue
  [ -z "$metric" ] && metric=99
  [ "$disabled" = "1" ] && continue
  printf '%d|%s\n' "$metric" "$interface"
done | sort -t'|' -k1 -n > "$_TMP"

# the member the routing table actually prefers: lowest metric among live ones
KM_ACTIVE=""
KM_ACTIVE_METRIC=999
_TMP2=/tmp/kmwan_members.$$
while IFS='|' read -r metric interface; do
  UP=""
  if [ "$interface" = "modem_0001" ]; then
    # cellular modem has no ubus network interface; use AT gate instead
    [ "$CELL_OK" = "1" ] && UP="true" || UP="false"
  else
    UP=$(ubus call network.interface.$interface status 2>/dev/null | sed -n 's/.*"up": \(true\|false\).*/\1/p' | head -1)
    [ -z "$UP" ] && UP=false
  fi
  case "$interface" in
    wan) label="WAN PORT" ;;
    wwan) label="Repeater" ;;
    modem_0001) label="Cellular" ;;
    tethering) label="Tethering" ;;
    secondwan) label="ETH2" ;;
    *) label="$interface" ;;
  esac
  printf '%s|%s|%d|%s\n' "$label" "$interface" "$metric" "$UP"
  if [ "$UP" = "true" ] && [ "$metric" -lt "$KM_ACTIVE_METRIC" ]; then
    KM_ACTIVE="$interface"; KM_ACTIVE_METRIC="$metric"
  fi
done < "$_TMP" > "$_TMP2"

KM_MEMBERS_JSON=$(awk -F'|' '{
  printf "%s{\"name\":\"%s\",\"interface\":\"%s\",\"metric\":%d,\"up\":%s}", sep, $1, $2, $3, $4
  sep=","
}' < "$_TMP2")
KM_MEMBERS_JSON="[$KM_MEMBERS_JSON]"

rm -f "$_TMP" "$_TMP2"
[ -z "$KM_ACTIVE" ] && KM_ACTIVE="none"

# Ada 01-Nov-2026 uptime for the currently active kmwan member. The modem has
# no ubus network interface, so cellular shows null.
KM_ACTIVE_UPTIME=""
if [ "$KM_ACTIVE" != "modem_0001" ] && [ "$KM_ACTIVE" != "none" ]; then
  KM_ACTIVE_UPTIME=$(ubus call network.interface.$KM_ACTIVE status 2>/dev/null | sed -n 's/.*"uptime": \([0-9]*\).*/\1/p' | head -1)
fi
[ -z "$KM_ACTIVE_UPTIME" ] && KM_ACTIVE_UPTIME="null"

# --- firmware identity --------------------------------------------------------
# Ada 24-Oct-2026 the resources box now carries the router's firmware (Matt). GL.iNet
# keeps it in three files. NOTE /etc/version.date is the build time in the
# BUILDER's local time (UTC+8), and the router's own web UI displays it as UTC --
# the page converts it the same way rather than printing a wrong hour.
ROUTER_FW_VER=$(cat /etc/glversion 2>/dev/null | tr -d '\r\n')
ROUTER_FW_TYPE=$(cat /etc/version.type 2>/dev/null | tr -d '\r\n')
ROUTER_FW_DATE=$(cat /etc/version.date 2>/dev/null | tr -d '\r\n')
# Ada 24-Oct-2026 the inputs for the firmware-UPDATE check (Matt). GL.iNet's
# firmware API needs the hardware model (NOT "GL-X3000" -- the bare "x3000" from
# /proc/gl-hw-info/model), the build number, the country code and the timezone.
# See check_firmware() in poll.py.
HW_MODEL=$(cat /proc/gl-hw-info/model 2>/dev/null | tr -d '\r\n')
FW_BUILD=$(cat /etc/version.build 2>/dev/null | tr -d '\r\n')
COUNTRY_CODE=$(cat /proc/gl-hw-info/country_code 2>/dev/null | tr -d '\r\n')
TZ_NAME=$(uci -q get 'system.@system[0].zonename' 2>/dev/null)

# glmodem stores the SIM's ICCID as the section name; derive it the same way.
GLMODEM_SECTION=$(uci show glmodem 2>/dev/null | sed -n 's/^\(glmodem\.[0-9]\{15,\}\)=sim$/\1/p' | head -1)
GLMODEM_ICCID=$(uci -q get "$GLMODEM_SECTION.iccid" 2>/dev/null)
GLMODEM_APN=$(uci -q get "$GLMODEM_SECTION.apn" 2>/dev/null)

WAN_UP=$(ubus call network.interface.wan status 2>/dev/null | sed -n 's/.*"up": \(true\|false\).*/\1/p' | head -1)
TETH_UP=$(ubus call network.interface.tethering status 2>/dev/null | sed -n 's/.*"up": \(true\|false\).*/\1/p' | head -1)

# --- emit -------------------------------------------------------------------
# json key/value emitters: s() quotes, n() quotes-or-null, b() bare true/false
# q() escapes for JSON. The modem replies carry their own embedded quotes
# (e.g. +QCSQ: "LTE",-81,-108,2,-14), so stripping them is not enough -- they
# must be escaped or the JSON breaks.
q() {
  _k=$1; _v=${2:-}
  _e=$(printf '%s' "$_v" | sed 's/\\/\\\\/g; s/"/\\"/g')
  printf '"%s":"%s"' "$_k" "$_e"
}
s() { q "$@"; }
n() { if [ -n "${2:-}" ]; then q "$@"; else printf '"%s":null' "$1"; fi; }
b() { if [ "$2" = "1" ] || [ "$2" = "true" ]; then printf '"%s":true' "$1"; else printf '"%s":false' "$1"; fi; }

printf '{'
s collected_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf ','
n uptime "$UPTIME"
printf ','
s load "$LOAD"
printf ','
n mem_total "$MEM_TOTAL"
printf ','
n mem_free "$MEM_FREE"
printf ','
n mem_avail "$MEM_AVAIL"
printf ','
n overlay_pct "$OVERLAY_PCT"
printf ','
n temp_c "$TEMP"
printf ','
n wwan_uptime "$WAN_UPTIME"
printf ','
s wwan_addr "$WAN_ADDR"
printf ','
s wwan_gateway "$WAN_GW"
printf ','
s wwan_dns "$WAN_DNS"
printf ','
n wwan_metric "$WAN_METRIC"
printf ','
s wwan_device "$WAN_DEV"
printf ','
n rx_bytes "$RX"
printf ','
n tx_bytes "$TX"
printf ','
s ssid "$SSID"
printf ','
s ap "$AP"
printf ','
n channel "$CHANNEL"
printf ','
n freq_ghz "$FREQ"
printf ','
n bitrate "$RATE"
printf ','
n signal_dbm "$SIGNAL"
printf ','
n noise_dbm "$NOISE"
printf ','
n link_quality "$QUALITY"
printf ','
n link_quality_max "$QUALITY_MAX"
printf ',' 
s kmwan_mode "$KM_MODE"
printf ',' 
s kmwan_level "$KM_LEVEL"
printf ',' 
n kmwan_sensitivity "$KM_SENS"
printf ',' 
s kmwan_active "$KM_ACTIVE"
printf ',' 
n kmwan_active_metric "$KM_ACTIVE_METRIC"
printf ',' 
n kmwan_active_uptime "$KM_ACTIVE_UPTIME"
printf ',' 
b kmwan_wan_up "$WAN_UP"
printf ','
b kmwan_tethering_up "$TETH_UP"
printf ','
printf '"kmwan_members":%s' "$KM_MEMBERS_JSON"
printf ','
s at_port "${ATPORT:-}"
printf ','
b cell_ok "$CELL_OK"
printf ','
s csq "$CSQ"
printf ','
s qcsq "$QCSQ"
printf ','
s qnwinfo "$QNWINFO"
printf ','
s qeng "$QENG"
printf ','
s qcainfo "$QCAINFO"
printf ','
s cops "$COPS"
printf ','
s cclk "$CCLK"
printf ','
s imsi "$CIMI"
printf ','
s fw_version "$FWVER"
printf ','
s router_fw_ver "$ROUTER_FW_VER"
printf ','
s router_fw_type "$ROUTER_FW_TYPE"
printf ','
s router_fw_date "$ROUTER_FW_DATE"
printf ','
s hw_model "$HW_MODEL"
printf ','
s fw_build "$FW_BUILD"
printf ','
s country_code "$COUNTRY_CODE"
printf ','
s zonename "$TZ_NAME"
printf ','
s cereg "$CEREG"
printf ','
s apn "$GLMODEM_APN"
printf ','
s iccid "$GLMODEM_ICCID"
printf '}\n'
