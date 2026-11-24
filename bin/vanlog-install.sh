#!/bin/sh
## ada 23-Nov-2026 install the Delilah black box (vanlog.sh) onto the device.
##
## Run this FROM HARLAN once Delilah's login shell is back (the upgrade removed
## zsh, which is what breaks ssh exec sessions; until that is fixed nothing can be
## installed). Idempotent: safe to run twice.
##
## Note on the transport: Delilah's dropbear has no sftp-server, so plain `scp`
## fails. We stream the file with `cat >` over ssh, which dropbear does support,
## and keep a checksum comparison as proof the copy landed intact.

set -eu
HOST=${VANLOG_HOST:-root@delilah.cinnamon-pinecone.ts.net}
SRC=$(dirname "$0")/vanlog.sh
[ -f "$SRC" ] || { echo "vanlog.sh not found beside $0" >&2; exit 1; }

echo "== installing vanlog.sh on $HOST =="
SUM_LOCAL=$(md5sum "$SRC" | awk '{print $1}')

ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" \
    'mkdir -p /etc/vanlog && chmod 755 /etc/vanlog' < /dev/null

ssh -o BatchMode=yes "$HOST" 'cat > /etc/vanlog/vanlog.sh' < "$SRC"
ssh -o BatchMode=yes "$HOST" 'chmod 755 /etc/vanlog/vanlog.sh'

SUM_REMOTE=$(ssh -o BatchMode=yes "$HOST" 'md5sum /etc/vanlog/vanlog.sh' | awk '{print $1}')
echo "   local md5 : $SUM_LOCAL"
echo "   device md5: $SUM_REMOTE"
[ "$SUM_LOCAL" = "$SUM_REMOTE" ] || { echo "CHECKSUM MISMATCH - not wiring cron" >&2; exit 1; }

# 1. run it once now, so the first line exists even before cron fires
echo "== first run =="
ssh -o BatchMode=yes "$HOST" '/etc/vanlog/vanlog.sh; echo "rc=$?"'

# 2. cron: every minute. busybox crond reads /etc/crontabs/root
echo "== wiring cron =="
ssh -o BatchMode=yes "$HOST" 'grep -q vanlog.sh /etc/crontabs/root 2>/dev/null || echo "* * * * * /etc/vanlog/vanlog.sh" >> /etc/crontabs/root; /etc/init.d/cron restart >/dev/null 2>&1 || crond -l 9 >/dev/null 2>&1; crontab -l 2>/dev/null | tail -3'

# 3. survive a settings-keeping firmware flash: /etc/vanlog and the crontab are
#    outside what sysupgrade keeps by default, so name them explicitly
echo "== adding to /etc/sysupgrade.conf (survives a keep-settings flash) =="
ssh -o BatchMode=yes "$HOST" 'for p in /etc/vanlog /etc/crontabs/root; do grep -qxF "$p" /etc/sysupgrade.conf 2>/dev/null || echo "$p" >> /etc/sysupgrade.conf; done; cat /etc/sysupgrade.conf | tail -5'

echo
echo "== done. First lines on the device: =="
ssh -o BatchMode=yes "$HOST" 'tail -5 /etc/vanlog/vanlog.log 2>/dev/null'
echo
echo "Read it back any time with:  vanlog-fetch.sh   (or  ssh $HOST tail -50 /etc/vanlog/vanlog.log)"
