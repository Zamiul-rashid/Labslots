#!/usr/bin/env bash
# Builds the stress lab's server side inside a fresh Docker host (CT 190): test CA, Headscale, the labslots
# hub, slot agent and scheduler sidecar, and the seeded schedule. Run from /root/stress with the labslots
# source in ./src.
#
#   ./setup.sh                          single-host run (70 PCs, 57 groups, 80 students): then python3 run.py
#   FRONT=<ip> RESET=1 SEED=scale ./setup.sh
#                                       multi-host run: devices on other machines reach Headscale at FRONT
#                                       (a TCP forwarder in front of this CT); scale.py seeds and drives it
#
# Lab PCs use Tailscale SSH: the scheduler logs in as labsched with no key, so setup-lab-pc.sh is used as is.
set -euo pipefail
cd "$(dirname "$0")"
HS_IMAGE=headscale/headscale:0.29.3
CT_IP=$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1)
FRONT=${FRONT:-$CT_IP}
grep -q 'hs-stress.test' /etc/hosts || echo "$CT_IP hs-stress.test" >> /etc/hosts

if [ "${RESET:-0}" = 1 ]; then   # a clean run: forget every node, user, key and booking of earlier runs
  docker compose -f core-compose.yml down --remove-orphans >/dev/null 2>&1 || true
  docker rm -f stress-hs >/dev/null 2>&1 || true
  rm -rf hs hub-data agent-data ts-state secrets   # the test CA stays: device machines already trust it
fi
mkdir -p certs hs/config hs/data hs/run hub-data agent-data ts-state secrets/agent secrets/tailscale results

# --- test CA and Headscale's TLS certificate (valid for the name, this CT, the front and localhost) ---------
if [ ! -f certs/hs.crt ] || ! grep -q "IP:$FRONT," certs/san.ext; then
  [ -f certs/ca.key ] || openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=stress-lab test CA" \
    -keyout certs/ca.key -out certs/ca.crt 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=hs-stress.test" -keyout certs/hs.key -out certs/hs.csr 2>/dev/null
  printf "subjectAltName=DNS:hs-stress.test,IP:%s,IP:%s,IP:127.0.0.1\n" "$FRONT" "$CT_IP" > certs/san.ext
  openssl x509 -req -in certs/hs.csr -CA certs/ca.crt -CAkey certs/ca.key -CAcreateserial -days 30 \
    -extfile certs/san.ext -out certs/hs.crt 2>/dev/null
  chmod 644 certs/*.crt certs/hs.key
fi

# --- Headscale --------------------------------------------------------------------------------------
sed "s/__FRONT__/$FRONT/g" headscale.yaml > hs/config/config.yaml
docker rm -f stress-hs >/dev/null 2>&1 || true
docker run -d --name stress-hs --network host --restart unless-stopped \
  -v "$PWD/hs/config:/etc/headscale" -v "$PWD/hs/data:/var/lib/headscale" -v "$PWD/hs/run:/var/run/headscale" \
  -v "$PWD/certs:/certs:ro" "$HS_IMAGE" serve >/dev/null
for i in $(seq 1 60); do docker exec stress-hs headscale users list >/dev/null 2>&1 && break; sleep 1; done
docker exec stress-hs headscale version
hs() { docker exec stress-hs headscale "$@"; }
[ -s secrets/agent/headscale_api_key ] || hs apikeys create -e 30d | tail -1 > secrets/agent/headscale_api_key

# --- labslots hub, seeded schedule, slot agent --------------------------------------------------------
docker build -q -t labslots:stress src >/dev/null
chown -R 10001 hub-data agent-data
docker compose -f core-compose.yml up -d hub
for i in $(seq 1 60); do curl -sf http://127.0.0.1:3100/slots/healthz >/dev/null && break; sleep 1; done
if [ "${SEED:-single}" = scale ]; then python3 corectl.py seed "${N_PCS:-70}" "${N_GROUPS:-140}" "${N_USERS:-700}"
else python3 run.py seed; fi
docker exec stress-hub python -c "
from labslots.db import DB; from labslots.hub import add_server
print(add_server(DB('/data/labslots.db'), 'hs1'))" | tail -1 > secrets/agent/agent_token
chown -R 10001 secrets/agent && chmod 500 secrets/agent && chmod 400 secrets/agent/*
docker compose -f core-compose.yml up -d agent
echo "waiting for the agent to write the first policy (tag owners for the PCs and the scheduler)"
for i in $(seq 1 60); do hs policy get 2>/dev/null | grep -q '"ssh"' && break; sleep 2; done
hs policy get | grep -c '"tag:pc-' | sed 's/^/policy tags for PCs: /'

# --- scheduler sidecar (tag:scheduler) --------------------------------------------------------------
if [ ! -s secrets/tailscale/ts_authkey ]; then
  hs preauthkeys create --tags tag:scheduler --reusable -e 24h -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])' \
    > secrets/tailscale/ts_authkey
  chmod 700 secrets/tailscale && chmod 600 secrets/tailscale/ts_authkey
fi
docker compose -f core-compose.yml up -d ts-scheduler

# --- the simulated lab PC image (single-host runs; scale.py builds it on the device machines) ----------
cp src/labslots/pc/setup-lab-pc.sh pc/setup-lab-pc.sh
docker build -q -t stress-pc pc >/dev/null
docker pull -q tailscale/tailscale:v1.102.3 >/dev/null
echo "stress lab ready: Headscale at https://$FRONT:8443 (this CT: $CT_IP), hub on :3100"
