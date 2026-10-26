#!/usr/bin/env bash
#
# Prepare one lab PC for slot switching by the labslots scheduler.
#
#   sudo bash setup-lab-pc.sh <admin-user> [<admin-user> ...] [--harden]
#
# The PC must already be on its Headscale server with its tag:pc-... tag.
#
# What it does (idempotent, safe to re-run):
#   * turns on Tailscale SSH (`tailscale set --ssh`): over the tailnet, port 22 is answered by
#     tailscaled, which checks the caller's tailnet identity against the policy's `ssh` rules.
#     Nobody needs an SSH key: admins may log in as any non-root account, the slot agent
#     (tag:scheduler) as `labsched`, and the group in its slot as its own grp-* account.
#   * creates the `labsched` account (no password, no key) and the groups `slot-active`
#     (the group in its slot) and `lab-admins`, and adds the admin users you name to lab-admins
#   * installs /usr/local/sbin/slot-switch, which `labsched` may run with sudo (nothing else)
#   * removes the SSH keys an older version of this script installed for labsched and grp-*
#   * with --harden: OpenSSH (still used from the LAN) only admits lab-admins and slot-active,
#     key-only, and (if ufw is active) SSH only on tailscale0.
#     Name at least one admin user, or you can lock yourself out.
#
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
HARDEN=0; ADMINS=()
for a in "$@"; do
  case "$a" in
    --harden) HARDEN=1 ;;
    -*) echo "unknown option $a"; exit 1 ;;
    *) id "$a" >/dev/null 2>&1 || { echo "no such user: $a"; exit 1; }; ADMINS+=("$a") ;;
  esac
done
[[ ${#ADMINS[@]} -gt 0 ]] || { echo "usage: sudo bash $0 <admin-user> [...] [--harden]"; exit 1; }
command -v tailscale >/dev/null || { echo "tailscale is not installed: join this PC to Headscale first"; exit 1; }
tailscale status >/dev/null 2>&1 || { echo "tailscale is not connected: run tailscale up (with this PC's tag) first"; exit 1; }

tailscale set --ssh

getent group slot-active >/dev/null || groupadd slot-active
getent group lab-admins  >/dev/null || groupadd lab-admins
for u in "${ADMINS[@]}"; do usermod -aG lab-admins "$u"; done

id labsched >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash --comment "labslots scheduler" labsched
passwd -l labsched >/dev/null

# Keys from the key-based version of this script: Tailscale SSH does not use them, and OpenSSH
# on the LAN should not accept them.
for u in labsched $(getent passwd | cut -d: -f1 | grep -E '^grp-[a-z0-9-]+$' || true); do
  home=$(getent passwd "$u" | cut -d: -f6)
  if [[ -n "$home" && -f "$home/.ssh/authorized_keys" ]]; then
    rm -f "$home/.ssh/authorized_keys"; echo "removed old SSH keys of $u"
  fi
done

cat > /usr/local/sbin/slot-switch <<'HELPER'
#!/usr/bin/env bash
# slot-switch <grp-account|->
# Makes <account> the only member of slot-active (creating it on first use, so Tailscale SSH
# can log the group in) and ends every other grp-* account's sessions and processes.
set -euo pipefail
IN="${1:-}"
[[ "$IN" == "-" || "$IN" =~ ^grp-[a-z0-9-]{1,28}$ ]] || { echo "bad account: $IN" >&2; exit 2; }

for u in $(getent passwd | cut -d: -f1 | grep -E '^grp-[a-z0-9-]+$' || true); do
  [[ "$u" == "$IN" ]] && continue
  gpasswd -d "$u" slot-active >/dev/null 2>&1 || true
  loginctl terminate-user "$u" >/dev/null 2>&1 || true
  pkill -KILL -u "$u" >/dev/null 2>&1 || true
done

if [[ "$IN" != "-" ]]; then
  id "$IN" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --comment "thesis group account" "$IN"
  gpasswd -a "$IN" slot-active >/dev/null
fi
echo "slot-active=${IN} on $(hostname)"
HELPER
chmod 755 /usr/local/sbin/slot-switch; chown root:root /usr/local/sbin/slot-switch

echo 'labsched ALL=(root) NOPASSWD: /usr/local/sbin/slot-switch' > /etc/sudoers.d/labslots
chmod 440 /etc/sudoers.d/labslots
visudo -cf /etc/sudoers.d/labslots >/dev/null

if [[ $HARDEN -eq 1 ]]; then
  cat > /etc/ssh/sshd_config.d/50-labslots.conf <<'SSHD'
# labslots: only admins and the group in its slot may log in through OpenSSH.
# Tailnet logins go through Tailscale SSH and the policy instead.
AllowGroups lab-admins slot-active
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
SSHD
  sshd -t
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow in on tailscale0 to any port 22 proto tcp >/dev/null
    ufw delete allow 22/tcp >/dev/null 2>&1 || true
    ufw delete allow OpenSSH >/dev/null 2>&1 || true
  fi
  echo "sshd hardened. Check you can still log in from another terminal before closing this one."
fi

echo "Done. Tailscale SSH is on; labsched, slot-active, lab-admins (${ADMINS[*]}) and /usr/local/sbin/slot-switch are ready."
echo "Check from an admin device on the tailnet: ssh <your-user>@<this PC> should log in with no key or password."
