#!/bin/sh
## ada 23-Nov-2026 read the Delilah black box back to harlan and summarise it.
##
## Streams /etc/vanlog/vanlog.log (plus rotations) off the device -- over ssh, which
## works even when the login shell is broken IF dropbear can still spawn it; when ssh
## is dead entirely, the file is also reachable over sftp or the UI's file browser,
## because it lives on the overlay as plain text.
##
## Prints a summary first (boots, uplink transitions, member switches, loss), then
## the raw tail. The summary is the point: ~10 % of the lines carry ~90 % of the
## answer, and reading 20k lines by eye is how we miss the one that mattered.

set -eu
HOST=${VANLOG_HOST:-root@delilah.cinnamon-pinecone.ts.net}
DEST=${VANLOG_DEST:-/home/ada/van/var/vanlog-delilah.log}
LINES=${VANLOG_LINES:-400}

ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" \
    'cat /etc/vanlog/vanlog.log /etc/vanlog/vanlog.log.1 /etc/vanlog/vanlog.log.2 2>/dev/null' > "$DEST" || {
        echo "ssh fetch failed (shell or link down). File remains on the device at";
        echo "  /etc/vanlog/vanlog.log  -- reachable later over ssh, sftp, or the UI.";
        exit 1;
    }
echo "fetched $(wc -l < "$DEST") lines -> $DEST"
echo

echo "== boots (a reboot is never inferred again) =="
grep -n 'boot marker' "$DEST" | tail -10 || echo "  none in this window"

echo
echo "== uplink transitions (wwan up/down, wan, tethering) =="
awk '/ ev=1 /{
  split($0,f," ");
  for(i=1;i<=NF;i++){ if(f[i] ~ /^wwan=/) w=f[i]; if(f[i] ~ /^wan=/) e=f[i]; if(f[i] ~ /^teth=/) t=f[i]; if(f[i] ~ /^sig=/) s=f[i]; if(f[i] ~ /^members=/) m=f[i]; if(f[i] ~ /^ping=/) p=f[i]; }
  key=w" "e" "t" "m;
  if(key!=prev){ printf "  %s  %s %s %s %s %s\n", $1, w, e, t, m, p; prev=key }
}' "$DEST" | tail -40

echo
echo "== reachability losses (probe loss > 0 or unknown) =="
grep ' ev=1 ' "$DEST" | awk '{for(i=1;i<=NF;i++) if($i ~ /^ping=/) p=$i} p !~ /^ping=0\// {print "  " $1 " " $2 " " p}' | tail -30

echo
echo "== kmwan member / cell-cap config lines seen =="
grep ' ev=1 ' "$DEST" | awk '{for(i=1;i<=NF;i++){ if($i ~ /^lim1=/) l1=$i; if($i ~ /^lim2=/) l2=$i; if($i ~ /^limday=/) ld=$i; if($i ~ /^creg=/) c=$i; if($i ~ /^cereg=/) c2=$i } key=l1" "l2" "ld" "c" "c2; if(key!=prev){ printf "  %s  %s %s %s %s %s\n", $1, l1, l2, ld, c, c2; prev=key }}' "$DEST" | tail -20

echo
echo "== kernel/daemon lines logged =="
grep -c '\[kern\]' "$DEST" | sed 's/^/  lines: /'
grep '\[kern\]' "$DEST" | tail -20

echo
echo "== raw tail ($LINES lines) =="
tail -"$LINES" "$DEST"
