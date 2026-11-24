#!/bin/sh
## Ada 24-Nov-2026  Renew Delilah's tailnet (Let's Encrypt) cert into nginx, and swap it in
##                  only when the bytes actually change. Installed at /etc/le-cert-renew.sh.
##
## Why it looks like this: for a *.ts.net name Tailscale IS the ACME client (DNS-01 in its own
## zone), so there is nothing to run here but `tailscale cert`. tailscaled returns the cert it
## already holds when that cert still satisfies --min-validity, so a scheduled run is a no-op
## most weeks and only touches nginx when a real renewal happened.
##
## Also covers the post-flash regression: if a firmware flash restores the vendor self-signed
## pair over these paths, the bytes differ and the next run puts the LE cert back.
##
## Run from /etc/crontabs/root (weekly). Silent by default; appends to /etc/le-cert-renew.log
## only when it did something. Keep this file, the crontab and the cert paths listed in
## /etc/sysupgrade.conf so a keep-settings flash does not drop them.
##
## Manual rollback to the vendor cert:
##   cp -a /etc/nginx/nginx.cer.gl-selfsigned-20261124 /etc/nginx/nginx.cer
##   cp -a /etc/nginx/nginx.key.gl-selfsigned-20261124 /etc/nginx/nginx.key
##   nginx -t && nginx -s reload
## NOTE: /etc/init.d/nginx has NO reload action on this firmware (it exits 0 and does nothing),
## so always reload with `nginx -s reload`.

DOMAIN=delilah.cinnamon-pinecone.ts.net
CERT=/etc/nginx/nginx.cer
KEY=/etc/nginx/nginx.key
NEW_CERT="$CERT.new"
NEW_KEY="$KEY.new"
MIN_VALIDITY="${LE_MIN_VALIDITY:-720h}"   # renew once less than this is left (90-day LE certs)
LOG=/etc/le-cert-renew.log

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

tailscale cert --min-validity="$MIN_VALIDITY" --cert-file "$NEW_CERT" --key-file "$NEW_KEY" "$DOMAIN" >/dev/null 2>&1
rc=$?
if [ $rc -ne 0 ]; then
  rm -f "$NEW_CERT" "$NEW_KEY"
  log "FAILED: tailscale cert rc=$rc, cert in place left untouched"
  exit 0
fi

# Same bytes = tailscaled handed back the cert it already had = nothing to do.
if cmp -s "$NEW_CERT" "$CERT"; then
  rm -f "$NEW_CERT" "$NEW_KEY"
  exit 0
fi

cp -a "$CERT" "$CERT.prev" 2>/dev/null
cp -a "$KEY"  "$KEY.prev"  2>/dev/null
chmod 644 "$NEW_CERT"
chmod 600 "$NEW_KEY"
mv "$NEW_CERT" "$CERT"
mv "$NEW_KEY"  "$KEY"

if nginx -t >/dev/null 2>&1; then
  nginx -s reload
  log "swapped in $DOMAIN, now valid until: $(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2)"
else
  log "nginx -t FAILED after swap; restoring the previous pair"
  cp -a "$CERT.prev" "$CERT"
  cp -a "$KEY.prev"  "$KEY"
  nginx -t >/dev/null 2>&1 && nginx -s reload
fi
