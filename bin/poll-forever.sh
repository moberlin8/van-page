#!/bin/sh
# ## Ada 22-Oct-2026 run the /van collector every 60s. Started by
# /etc/systemd/user/van-poll.service. One tick per invocation; the loop is here
# rather than in the service so a long ssh hang cannot queue ticks behind it.
INTERVAL=60
while :; do
  /home/ada/starlink/.venv/bin/python3 /home/ada/van/bin/poll.py \
      >>/home/ada/van/var/poll.log 2>&1
  sleep "$INTERVAL"
done
