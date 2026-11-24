#!/bin/sh
# Ada 06-Nov-2026 workstream E: Delilah's log, off the device (plan §8).
#
# The router keeps a 64 KB ring buffer and no remote sink, so anything that happens while nobody
# is watching is gone within the hour. This follows the log over ssh, drops the noise the router
# generates by itself, and keeps a rotating file the page (and I) can read afterwards.
#
# The noise list is measured, not guessed: on 06-Nov-2026 the 521-line buffer was 136 lines of our
# own dropbear sessions, 224 lines of the modem's ipv6 retry block, 56 lines of udhcpc renewals and
# ~50 lines of wifi driver warnings. Everything else is kept, and every 200 dropped lines a marker
# says how many were dropped, so nothing disappears silently.
LOG=${VAN_ROUTER_LOG:-/srv/van/data/router.log}
MAX=${VAN_ROUTER_LOG_MAX:-5000000}
KEEP=${VAN_ROUTER_LOG_KEEP:-3}
HOST=${VAN_ROUTER_HOST:-root@delilah}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30"

stamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }

rotate() {
  [ -f "$LOG" ] || return 0
  size=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  [ "$size" -lt "$MAX" ] && return 0
  i=$KEEP
  while [ "$i" -gt 1 ]; do
    [ -f "$LOG.$((i - 1))" ] && mv -f "$LOG.$((i - 1))" "$LOG.$i"
    i=$((i - 1))
  done
  mv -f "$LOG" "$LOG.1"
}

noise() {
  case "$1" in
    *'dropbear['*) return 0 ;;
    *'kmwan: config json'*) return 0 ;;
    *'may not support ipv6'*|*CGPADDR*|*'trying to get ipv'*|*requestSetupDataCall*|*call_end_reason*) return 0 ;;
    *'udhcpc: sending renew'*|*'udhcpc: lease of'*) return 0 ;;
    *PeerAddBARspActionSanity*|*tx_free_v*|*'IN=br-lan OUT='*) return 0 ;;
  esac
  return 1
}

filter() {
  dropped=0
  while IFS= read -r line; do
    if noise "$line"; then
      dropped=$((dropped + 1))
      if [ "$dropped" -ge 200 ]; then
        printf '# filtered %s noise lines\n' "$dropped"
        dropped=0
      fi
    else
      printf '%s\n' "$line"
    fi
  done
  [ "$dropped" -gt 0 ] && printf '# filtered %s noise lines\n' "$dropped"
  return 0
}

# The first dump after a start can come back empty (a connect timeout, or a previous stream still
# holding the router's dropbear). Retry rather than record a silent gap: the buffer is the only
# record of what happened while the stream was down.
dump() {
  attempt=1
  while [ "$attempt" -le 3 ]; do
    out=$($SSH "$HOST" 'logread' 2>/dev/null)
    if [ -n "$out" ]; then
      printf '%s\n' "$out" | filter
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 3
  done
  printf '# ring buffer dump failed after 3 attempts %s\n' "$(stamp)"
}

mkdir -p "$(dirname "$LOG")"
while :; do
  rotate
  printf '# --- ring buffer dump %s ---\n' "$(stamp)" >> "$LOG"
  dump >> "$LOG"
  printf '# --- following %s ---\n' "$(stamp)" >> "$LOG"
  $SSH "$HOST" 'logread -f' 2>/dev/null | filter >> "$LOG"
  printf '# --- stream ended %s, reconnecting in 10s ---\n' "$(stamp)" >> "$LOG"
  sleep 10
done
