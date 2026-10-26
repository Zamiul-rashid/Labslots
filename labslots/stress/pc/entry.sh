#!/bin/bash
# Simulated lab PC: Tailscale in userspace mode with Tailscale SSH, prepared with the real setup-lab-pc.sh
# (run here, not at build time, because it turns Tailscale SSH on and needs a connected tailscaled).
# Every login on the tailnet goes through Tailscale SSH: there is no sshd and no key.
set -e
tailscaled --tun=userspace-networking --statedir=/var/lib/tailscale --port=0 --no-logs-no-support &
for i in $(seq 1 50); do [ -S /var/run/tailscale/tailscaled.sock ] && break; sleep 0.2; done
# The tag (tag:pc-N) comes with the pre-auth key; asking for it again with --advertise-tags is refused.
until tailscale up --login-server="$TS_LOGIN" --authkey="$TS_AUTHKEY" --hostname="$TS_HOSTNAME" \
    --accept-dns=false --ssh; do sleep 5; done
bash /setup-lab-pc.sh labadmin
wait
