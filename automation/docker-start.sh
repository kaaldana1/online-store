#!/bin/sh
# Start a virtual screen (Xvfb), wait until it's ready, then run the worker on it.
# (xvfb-run can hang forever inside Docker waiting for a "ready" signal, so we do it ourselves.)
set -e
export DISPLAY=:99
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99     # left over if the container was restarted
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp 2>/dev/null &
i=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
  i=$((i + 1))
  if [ "$i" -gt 50 ]; then echo "Virtual screen (Xvfb) did not start within 10s."; exit 1; fi
  sleep 0.2
done
echo "Virtual screen ready."
exec python automation/poshmark.py worker --idle-if-off
