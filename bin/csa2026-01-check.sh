#!/bin/sh
# Ada 23-Nov-2026  read-only check for GL.iNet CSA-2026-01 (GoodCloud incident, 2026-11-20)
#                  plus the older urandom_seed backdoor. Adapted for Delilah (GL-X3000 4.8.3).
#                  Changes nothing on the device. Read-only by design: no netstat (a replaced
#                  netstat is a known trojan that starts the malware), no third-party script.
#
# Usage:  ssh root@HOST 'sh -s' < csa2026-01-check.sh
#
# Indicators from: gl-inet.com notice 2026-11-20; RemoteToHome-io/gl-security-incident-2026
# (docs/router-reverse-shell.md, docs/findings.md R-1..R-33); admonstrator/glinet-csa-2026-01 v2.52.

BB=/bin/busybox
[ -x "$BB" ] || BB=""
hits=0; rev=0
say()  { printf '%s\n' "$*"; }
hit()  { hits=$((hits+1)); say "  HIT   $*"; }
revw() { rev=$((rev+1));  say "  REVIEW $*"; }
ok()   { say "  clean $*"; }

# attackers' addresses, and 170.39.226.219 as it appears little-endian in /proc/net/tcp
ATTACK_IPS="170.39.226.219 66.245.219.252 65.20.71.131"
HEX_170="DBE227AA"

say "== device"
say "   $(cat /etc/glversion 2>/dev/null) / $(grep DISTRIB_DESCRIPTION /etc/openwrt_release 2>/dev/null | cut -d"'" -f2)"
say "   uptime: $($BB cat /proc/uptime 2>/dev/null | cut -d' ' -f1)s | model: $($BB cat /proc/gl-hw-info/model 2>/dev/null)"

say "== 1. the process GL.iNet tells you to look for (their published indicator)"
ps w 2>/dev/null | grep -F 'crond -i /tmp/.log' | grep -v grep >/dev/null 2>&1 && hit "process 'crond -i /tmp/.log' is running" || ok "(their indicator absent - note: absent on routers that DID have the shell)"

say "== 2. the reverse shell they did not publish (router attack, 20 Nov)"
ps w 2>/dev/null | grep -E 'mkfifo|nc [0-9]|sh -i|/tmp/f|SDWAN_IP_MAPPING' | grep -v grep >/dev/null 2>&1 \
  && { hit "shell-shaped process:"; ps w 2>/dev/null | grep -E 'mkfifo|nc [0-9]|sh -i|/tmp/f|SDWAN_IP_MAPPING' | grep -v grep | sed 's/^/        /'; } \
  || ok "no mkfifo/nc/sh -i/often-seen shell process"
$BB ls -la /tmp/f 2>/dev/null | grep -q . && hit "/tmp/f exists (the shell's FIFO; stays until reboot)" || ok "/tmp/f absent"

say "== 3. the firewall rule the cloud command left (nat chain SDWAN_IP_MAPPING)"
if command -v iptables >/dev/null 2>&1; then
  RULE=$(iptables -t nat -S SDWAN_IP_MAPPING 2>/dev/null; iptables -t nat -S 2>/dev/null | grep -F SDWAN_IP_MAPPING)
  case "$RULE" in
    *SDWAN_IP_MAPPING*) revw "SDWAN_IP_MAPPING chain present: $(printf '%s' "$RULE" | tr '\n' ' ')" ;;
    *) ok "no SDWAN_IP_MAPPING chain in nat (gone after a reboot)" ;;
  esac
else ok "iptables not present"; fi

say "== 4. the state file - the one trace that survives a reboot"
if [ -e /etc/mp_ip_mapping_info ]; then
  SZ=$($BB ls -l /etc/mp_ip_mapping_info 2>/dev/null)
  case "$SZ" in *"Nov 19"*|*"Nov 20"*|*"Nov 21"*|*"Nov 22"*|*"Nov 23"*) hit "/etc/mp_ip_mapping_info dated on/after 20 Nov -> the cloud handler ran here: $SZ" ;;
    *) revw "/etc/mp_ip_mapping_info exists: $SZ" ;; esac
else ok "/etc/mp_ip_mapping_info absent"; fi

say "== 5. outbound connections to the attackers' addresses (read from /proc, NOT netstat)"
$BB cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | grep -i "$HEX_170" >/dev/null 2>&1 && hit "live connection to 170.39.226.219" || ok "no live connection to 170.39.226.219"
for f in $(grep -rIl -e 170.39.226.219 -e 66.245.219.252 -e 65.20.71.131 -e gl-inet.top /etc/config /etc/init.d /etc/rc.local /etc/crontabs /etc/rtty_conf 2>/dev/null | head -5); do
  hit "attacker address/name in $f"
done

say "== 6. gl-helper file markers (the KVM implant's paths)"
for d in /etc/glinet/.cache /etc/config/.gl /etc/gl_nas/.cache /overlay/.gl /tmp/.gl; do
  [ -d "$d" ] && { revw "directory exists: $d"; $BB ls -la "$d" 2>/dev/null | sed 's/^/        /'; }
done
for f in gl-helper gb watch.sh run.sh wake.sh boot.sh rtty_keep.sh eco node .u .wc install.log; do
  for d in /etc/glinet/.cache /etc/config/.gl /etc/gl_nas/.cache /overlay/.gl /tmp/.gl; do
    [ -e "$d/$f" ] && hit "malware file: $d/$f"
  done
done
$BB ls /etc/glinet 2>/dev/null | grep -q . && revw "/etc/glinet present (its .cache is an implant path)" || ok "no /etc/glinet"

say "== 7. cron persistence the implants use"
for f in /etc/crontabs/root /tmp/gl_crontabs/root /etc/gl_crontabs/crontabs.d/*; do
  [ -f "$f" ] || continue
  LINES=$(grep -nE '\.gl|glinet/\.cache|tmp/\.log|watch\.sh|run\.sh' "$f" 2>/dev/null | head -4)
  [ -n "$LINES" ] && { printf '%s\n' "$LINES" | sed "s|^|        |"; hit "$f references implant paths/watchdogs"; }
done
[ -s /etc/crontabs/root ] && revw "/etc/crontabs/root is not empty: $(grep -c . /etc/crontabs/root) line(s) - read it" || ok "root crontab empty/absent"
if [ -s /etc/rc.local ]; then
  say "  REVIEW /etc/rc.local has content:"
  grep -vE '^\s*$|^\s*#' /etc/rc.local 2>/dev/null | head -6 | sed 's/^/        /'
  revw "rc.local is not empty (compare with the firmware's copy)"
else ok "rc.local empty"; fi

say "== 8. the rtty remote-terminal config (implant points it at 65.20.71.131)"
if [ -e /etc/rtty_conf ]; then
  grep -q '65.20.71.131' /etc/rtty_conf 2>/dev/null && hit "/etc/rtty_conf names the implant's relay 65.20.71.131" || revw "/etc/rtty_conf present: $(grep -c . /etc/rtty_conf 2>/dev/null) line(s) - note the addresses (relay addresses differ per device, so an unknown one alone is not proof)"
else ok "/etc/rtty_conf absent"; fi

say "== 9. the older backdoor (urandom_seed / ulogd / monitord / keep-list)"
for pair in /etc/init.d/urandom_seed /etc/rc.common /lib/upgrade/keep.d/netifd /etc/passwd /etc/rc.local; do
  if [ -e "/rom$pair" ] && [ -e "$pair" ]; then
    $BB cmp -s "/rom$pair" "$pair" 2>/dev/null && ok "$pair matches /rom" || revw "$pair DIFFERS from /rom - read it ($(wc -l < "$pair") lines vs $(wc -l < "/rom$pair"))"
  fi
done
[ -e /bin/ulogd ] && hit "/bin/ulogd present (older backdoor loader)" || ok "no /bin/ulogd"
[ -e /usr/bin/monitord ] && hit "/usr/bin/monitord present (copy of lua)" || ok "no /usr/bin/monitord"
ps w 2>/dev/null | grep -E 'monitord|ujaild' | grep -v grep >/dev/null 2>&1 && hit "monitord/ujaild process running" || ok "no monitord/ujaild process"
head -3 /etc/init.d/urandom_seed 2>/dev/null | grep -qE 'monitord|ulogd|cp /usr/bin/lua' && hit "/etc/init.d/urandom_seed has the backdoor's extra lines" || ok "urandom_seed head looks like firmware"
$BB ls -la /bin/netstat 2>/dev/null | grep -q busybox && ok "/bin/netstat is a busybox link (safe)" || revw "/bin/netstat is NOT a busybox link - do not run netstat, a replaced one starts the malware"

say "== 10. compiled Lua in the writable layer (cheap checks: the loader's exact sizes, then the magic)"
for sz in 33722c 35745c; do
  for f in $($BB find /overlay -type f -size $sz 2>/dev/null | head -10); do
    case "$f" in */usr/lib/lua/*|*/usr/lib/opkg/*|*/rom/*) ;; *) revw "file of the loader's exact size ($sz): $f" ;; esac
  done
done
for f in $($BB find /overlay/upper/etc /overlay/upper/bin /overlay/upper/usr/bin /overlay/upper/usr/sbin -type f 2>/dev/null | head -400); do
  case "$f" in */usr/lib/lua/*|*/opkg/*) continue ;; esac
  head -c 4 "$f" 2>/dev/null | grep -qa "Lua" && revw "compiled Lua in the writable layer: $f"
done

say "== 11. GoodCloud / cloud remote management (the attack path)"
uci show gl-cloud 2>/dev/null | grep -iE 'enable|bind' | $BB sed 's|^|   |' || say "   (no gl-cloud uci entries)"
ps w 2>/dev/null | grep -E 'gl-cloud|mpifd|rtty' | grep -v grep | sed 's/^/  REVIEW cloud process: /' | head -5
grep -n 'mpifd\|astro\|virtual_ip' /etc/config/* 2>/dev/null | head -5 | sed 's/^/  REVIEW /'

say ""
say "== VERDICT"
[ "${hits:-0}" -gt 0 ] && say "   ATTACK TRACES FOUND ($hits) - do NOT reboot: reboot destroys the process/FIFO evidence." \
                  || say "   No attack traces found (nothing conclusive: a rebooted device can look clean)."
[ "${rev:-0}" -gt 0 ] && say "   ${rev} item(s) worth a human read (this firmware is not stock, so differences are expected)."
exit 0
