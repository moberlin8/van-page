#!/bin/sh
# Ada 06-Nov-2026 workstream A: calibrate the router's per-SIM counter against a known transfer.
#
# The counter is the sum of the module's QGDCNT deltas, and three independent readings say it is in
# bytes - but "says" is not "measured against a known quantity". This downloads an exact number of
# bytes over the default route (so, over the cellular path) and compares the counter's delta with
# what actually arrived. The ratio is the answer: ~1.0 means bytes, ~0.001 means kilobytes, and
# anything else means the counter is not a simple byte count and the page's numbers need a caveat.
#
# It refuses to run on SIM1: that is a 100 MB one-shot card with about 1 MB left, and a 5 MB test
# would spend the remainder. Pass --force to spend it anyway.
#
# Usage: calibrate-sim.sh [bytes] [--force]
BYTES=${1:-5000000}
FORCE=0
[ "$2" = "--force" ] && FORCE=1
HOST=${VAN_ROUTER_HOST:-root@delilah}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10"

at() { $SSH "$HOST" "gl_modem -B 0001:01:00.0 AT '$1' 2>/dev/null" | tr -d '\r'; }

slot=$(at 'AT+QUIMSLOT?' | sed -n 's/.*+QUIMSLOT: *\([0-9][0-9]*\).*/\1/p' | head -1)
[ -z "$slot" ] && { echo "cannot read the active slot"; exit 1; }
if [ "$slot" = "1" ] && [ "$FORCE" -ne 1 ]; then
  echo "refusing: the modem is on SIM1, a 100 MB one-shot card with about 1 MB left."
  echo "switch to SIM2 first, or pass --force to spend SIM1's remainder."
  exit 2
fi

counter() { $SSH "$HOST" "cat /tmp/traffic_sim$slot 2>/dev/null || echo 0"; }

# The router's own resolver is not always healthy - on 06-Nov it answered NXDOMAIN for every name
# while IP traffic worked fine - so resolve here and hand curl the address. The test is about
# bytes, not about DNS, and a DNS failure would look like a failed transfer.
TARGET=speed.cloudflare.com
IP=$(getent ahostsv4 "$TARGET" | awk 'NR==1{print $1}')
[ -z "$IP" ] && { echo "cannot resolve $TARGET from here"; exit 1; }

# The modem is NOT the default route: kmwan's "wwan" member is the WiFi repeater (metric 1), the
# WAN jack is metric 2, and the cellular interface (rmnet_mhi0) is metric 3. A plain curl therefore
# goes over the repeater and the modem only sees background - which is exactly what the first run
# measured (5 MB transferred, 33 KB on the counter). Bind the source address of the cellular
# interface so the transfer actually rides the modem.
CELL_IF=${VAN_CELL_IF:-rmnet_mhi0}
CELL_IP=$($SSH "$HOST" "ip route show default | grep $CELL_IF | sed -n 's/.*src \\([0-9.]*\\).*/\\1/p'" | head -1)
[ -z "$CELL_IP" ] && { echo "cannot find the cellular interface's address ($CELL_IF)"; exit 1; }
echo "cellular source address: $CELL_IP (interface $CELL_IF)"

before=$(counter)
echo "slot $slot, counter before: $before"
# Bind the DEVICE, not the address: `--interface <ip>` still let the kernel pick the metric-1
# route (the repeater), and the counter saw nothing. `--interface rmnet_mhi0` is SO_BINDTODEVICE
# and does force the modem - measured 06-Nov: 3,000,000 bytes transferred, counter +3,183,308.
got=$($SSH "$HOST" "curl -sS --interface $CELL_IF --resolve $TARGET:443:$IP -o /dev/null -w '%{size_download}' 'https://$TARGET/__down?bytes=$BYTES' 2>/dev/null")
# The counter is written on a timer (the flash mirror runs every 30 s), so reading it the instant
# the transfer ends reports the value from before it. Settle first.
sleep ${VAN_CALIBRATE_SETTLE:-45}
after=$(counter)
echo "downloaded: $got bytes"
echo "counter after: $after"
awk -v b="$before" -v a="$after" -v g="$got" 'BEGIN {
  d = a - b;
  printf "counter delta: %d\n", d;
  if (g <= 0) { print "no bytes arrived: the transfer failed, nothing to calibrate"; exit }
  r = d / g;
  printf "ratio counter/bytes: %.5f\n", r;
  if (r > 0.9 && r < 1.2)        printf "=> the counter is in BYTES, over-counting the payload by %.1f%% (wire bytes, not payload)\n", (r - 1) * 100;
  else if (r > 0.0009 && r < 0.0012) print "=> the counter is in KILOBYTES";
  else                            print "=> unexpected ratio: the counter is not a plain byte count";
}'
