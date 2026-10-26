#!/bin/sh
# ## Ada 22-Oct-2026 hold the dish tunnel open: harlan:19200 -> delilah ->
# 192.168.100.1:9200. Started by the user unit van-tunnel.service.
# The tunnel is the page's ONLY path to the dish: harlan has no route to
# 192.168.100.0/24 and no wireless interface. If this is down the dish is dark,
# so it must survive drops and reboots.
while :; do
  echo "[tunnel] opening at $(date -Is)"
  ssh -N -o BatchMode=yes -o StrictHostKeyChecking=no \
      -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      -L 19200:192.168.100.1:9200 root@delilah \
      >>/home/ada/van/var/tunnel.log 2>&1
  echo "[tunnel] dropped rc=$? at $(date -Is); retrying in 10s"
  sleep 10
done
