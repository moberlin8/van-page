#!/bin/sh
## ada 23-Nov-2026 Delilah black box: one durable line a minute, on the overlay.
##
## WHY: the device's own log is a RAM ring buffer. A reboot erases it, and on a box
## this chatty it holds minutes, not an hour -- so a 50-minute absence is already
## overwritten by the time we can reach it. Our only read path is ssh, which is the
## very thing a firmware upgrade broke. This writes the evidence for the minutes we
## are blind, to /etc (the overlay), so it survives reboots and is readable later
## over any channel that works: ssh, sftp, or the UI's file browser.
##
## Output: one line per minute, plus one marker line per boot and one line per
## interesting kernel message. Plain text, greppable, no dependencies beyond
## busybox (sh, awk, sed, uci, ubus, logread, md5sum, iwinfo).
##
## Install: harlan:/home/ada/van/bin/vanlog-install.sh  (copies this file, wires
## cron, adds the paths to /etc/sysupgrade.conf so a settings-keeping flash keeps it)
## Read back: harlan:/home/ada/van/bin/vanlog-fetch.sh

LOG=${VANLOG_LOG:-/etc/vanlog/vanlog.log}
MAX=${VANLOG_MAX:-524288}       # rotate at 512 KiB
KEEP=${VANLOG_KEEP:-4}          # keep .1 .2 .3 .4
WM=${VANLOG_WM:-/etc/vanlog/.kernel.watermark}
DIR=${VANLOG_DIR:-/etc/vanlog}
# (LOG/WM/DIR are overridable only so this can be smoke-tested from harlan; the
#  device always takes the /etc/vanlog defaults)

[ -d "$DIR" ] || mkdir -p "$DIR" 2>/dev/null || exit 0

rotate() {
    [ -f "$LOG" ] || return 0
    size=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    [ "$size" -lt "$MAX" ] && return 0
    i=$KEEP
    while [ "$i" -gt 1 ]; do
        [ -f "$LOG.$((i - 1))" ] && mv -f "$LOG.$((i - 1))" "$LOG.$i" 2>/dev/null
        i=$((i - 1))
    done
    mv -f "$LOG" "$LOG.1" 2>/dev/null
}

# ---- identity / load -------------------------------------------------------
UP=$(cut -d. -f1 /proc/uptime 2>/dev/null)
LOAD=$(awk '{print $1","$2","$3}' /proc/loadavg 2>/dev/null)
MEMAV=$(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null)
TEMP=$(awk '{printf "%.0f", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
FW=$(cat /etc/glversion 2>/dev/null | tr -d '\r\n')

# ---- wwan: the repeater uplink (our normal path) ---------------------------
WWAN_STATUS=$(ubus call network.interface.wwan status 2>/dev/null)
WWAN_UP=$(echo "$WWAN_STATUS" | sed -n 's/.*"up": *\(true\|false\).*/\1/p' | head -1)
WWAN_UT=$(echo "$WWAN_STATUS" | sed -n 's/.*"uptime": *\([0-9]\{1,\}\).*/\1/p' | head -1)
WWAN_DEV=$(echo "$WWAN_STATUS" | sed -n 's/.*"l3_device": *"\([^"]*\)".*/\1/p' | head -1)
SIG=""; SSID=""; BR=""
if [ -n "$WWAN_DEV" ]; then
    WI=$(iwinfo "$WWAN_DEV" info 2>/dev/null)
    SIG=$(echo "$WI" | sed -n 's/.*Signal: *\(-\{0,1\}[0-9]\{1,3\}\) dBm.*/\1/p' | head -1)
    BR=$(echo "$WI" | sed -n 's/.*Bit Rate: *\([0-9.]\{1,\}\) MBit.*/\1/p' | head -1)
    SSID=$(echo "$WI" | sed -n 's/.*ESSID: *"\([^"]*\)".*/\1/p' | head -1)
fi

# ---- wan: the eth0 jack (where bypass-mode EvilDish will land) --------------
WAN_UP=$(ubus call network.interface.wan status 2>/dev/null | sed -n 's/.*"up": *\(true\|false\).*/\1/p' | head -1)
TETH_UP=$(ubus call network.interface.tethering status 2>/dev/null | sed -n 's/.*"up": *\(true\|false\).*/\1/p' | head -1)

# ---- kmwan members: config + live up-state (this is the switch we care about)
KM_MODE=$(uci -q get kmwan.global.mode 2>/dev/null)
KM_LEVEL=$(uci -q get kmwan.global.level 2>/dev/null)
MEMBERS=""
for section in $(uci show kmwan 2>/dev/null | sed -n 's/^\(kmwan\.[^.]*\)=member$/\1/p'); do
    iface=$(uci -q get "$section.interface" 2>/dev/null)
    metric=$(uci -q get "$section.metric" 2>/dev/null)
    dis=$(uci -q get "$section.disabled" 2>/dev/null)
    [ -z "$iface" ] && continue
    up=$(ubus call "network.interface.$iface" status 2>/dev/null | sed -n 's/.*"up": *\(true\|false\).*/\1/p' | head -1)
    MEMBERS="${MEMBERS}${MEMBERS:+,}${iface}:m${metric}:d${dis}:up${up:-?}"
done

# ---- cell: the modem's own cache, never the AT port ------------------------
# (talking to the port risks the uplink; /var/run/modem/at_cache.json is read-only)
AC=/var/run/modem/at_cache.json
CREG=""; CEREG=""; CSQ=""; QCSQ=""
if [ -f "$AC" ]; then
    CREG=$(sed -n 's/.*"CREG"[^"]*"[^"]*": *"\([^"]*\)".*/\1/p' "$AC" | head -1)
    CEREG=$(sed -n 's/.*"CEREG"[^"]*"[^"]*": *"\([^"]*\)".*/\1/p' "$AC" | head -1)
    CSQ=$(sed -n 's/.*"CSQ"[^"]*"[^"]*": *"\([^"]*\)".*/\1/p' "$AC" | head -1)
fi
SIM2_B=$(cat /tmp/traffic_sim2 2>/dev/null | tr -d '\r\n'); SIM2_F=$(cat /etc/gl_modem_traffic/traffic_sim2 2>/dev/null | tr -d '\r\n')
SIM1_B=$(cat /tmp/traffic_sim1 2>/dev/null | tr -d '\r\n'); SIM1_F=$(cat /etc/gl_modem_traffic/traffic_sim1 2>/dev/null | tr -d '\r\n')
# the data-cap gate itself: if this fires, kmwan can move members -- log its state
LIM_S1=$(uci -q get glmodem.modem_0001_traffic_limit.sim1_enable 2>/dev/null)/$(uci -q get glmodem.modem_0001_traffic_limit.sim1_threshold 2>/dev/null)
LIM_S2=$(uci -q get glmodem.modem_0001_traffic_limit.sim2_enable 2>/dev/null)/$(uci -q get glmodem.modem_0001_traffic_limit.sim2_threshold 2>/dev/null)
LIM_DAY=$(uci -q get glmodem.modem_0001_traffic_limit.sim2_day 2>/dev/null)/$(uci -q get glmodem.modem_0001_traffic_limit.sim2_hour 2>/dev/null)

# ---- our way in: tailscale + a real reachability probe ---------------------
TS_ADDR=$(ip -4 addr show dev tailscale0 2>/dev/null | sed -n 's/.*inet \([0-9.]*\)\/.*/\1/p' | head -1)
TS_PID=$(pidof tailscaled 2>/dev/null | awk '{print $1}')
PINGOUT=$(ping -c 2 -W 2 1.1.1.1 2>/dev/null | tail -2 | tr '\n' ' ')
PLOSS=$(echo "$PINGOUT" | sed -n 's/.* \([0-9]\{1,3\}\)% packet loss.*/\1/p')
PAVG=$(echo "$PINGOUT" | sed -n 's|.*= *[0-9.]*/\([0-9.]*\)/.*|\1|p')

# ---- LAN side: are the van's own clients up --------------------------------
AP_CLIENTS=$(iwinfo ra0 assoclist 2>/dev/null | grep -c '^[0-9A-Fa-f]\{2\}:')

# ---- interesting kernel/daemon lines, only the new ones --------------------
# Watermark = md5 of the last line we logged. Lines older than the watermark are
# skipped; if the watermark is gone (ring evicted it, or we just rebooted) we log
# what is there -- which is exactly what we want after a restart.
KLINES=$(logread 2>/dev/null | grep -iE 'mt_wifi|apcli|kmwan|disconn|deauth|BTM|out of memory|oom-killer|netifd: (wan|wwan|modem)' | tail -40)
if [ -n "$KLINES" ]; then
    LASTMD5=$(cat "$WM" 2>/dev/null)
    SAW=0
    [ -z "$LASTMD5" ] && SAW=1
    printf '%s\n' "$KLINES" | while IFS= read -r l; do
        if [ "$SAW" = "0" ]; then
            m=$(printf '%s' "$l" | md5sum | awk '{print $1}')
            [ "$m" = "$LASTMD5" ] && SAW=1
            continue
        fi
        printf '%s [kern] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$l" >> "$LOG"
    done
    printf '%s' "$(printf '%s\n' "$KLINES" | tail -1)" | md5sum | awk '{print $1}' > "$WM" 2>/dev/null
fi

# ---- the timed line --------------------------------------------------------
rotate
printf '%s ev=1 up=%s load=%s memav=%sK tempC=%s fw=%s wwan=%s/%ss/%s sig=%s br=%s ssid=%s wan=%s teth=%s km=%s/%s members=%s ping=%s%%/%sms tsaddr=%s tspid=%s clients=%s creg=%s cereg=%s csq=%s sim1=%s/%s sim2=%s/%s lim1=%s lim2=%s limday=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" \
    "${UP:-?}" "${LOAD:-?}" "${MEMAV:-?}" "${TEMP:-?}" "${FW:-?}" \
    "${WWAN_UP:-?}" "${WWAN_UT:-?}" "${WWAN_DEV:-none}" "${SIG:-?}" "${BR:-?}" "${SSID:-?}" \
    "${WAN_UP:-?}" "${TETH_UP:-?}" "${KM_MODE:-?}" "${KM_LEVEL:-?}" "${MEMBERS:-none}" \
    "${PLOSS:-?}" "${PAVG:-?}" "${TS_ADDR:-none}" "${TS_PID:-none}" "${AP_CLIENTS:-?}" \
    "${CREG:-?}" "${CEREG:-?}" "${CSQ:-?}" \
    "${SIM1_B:-?}" "${SIM1_F:-?}" "${SIM2_B:-?}" "${SIM2_F:-?}" \
    "${LIM_S1:-?}" "${LIM_S2:-?}" "${LIM_DAY:-?}" >> "$LOG"

# ---- boot marker: a reboot is a fact we must never have to infer ----------
if [ -n "$UP" ] && [ "$UP" -lt 300 ]; then
    printf '\n=== boot marker %s fw=%s uptime=%ss ===\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${FW:-?}" "$UP" >> "$LOG"
fi

exit 0
