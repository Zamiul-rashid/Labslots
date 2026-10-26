"""Builds the Headscale policy for one moment in the schedule.

Shape (Headscale 0.29 grants + Tailscale SSH rules):
  * group:admins             -> every lab PC, any port; Tailscale SSH as any non-root account
  * tag:scheduler            -> every lab PC, tcp:22; Tailscale SSH as labsched only (slot switching)
  * group:<slug> (in slot)   -> its PC's tag, tcp:22 plus the configured ports; Tailscale SSH as its
                                own group account only
  * plus the admin-maintained "extra grants" (e.g. autogroup:self, the jumphost) and "extra SSH rules"

Every SSH login to a PC is Tailscale SSH: nobody logs in with an SSH key. The PC's tailscaled answers
port 22 on the tailnet and checks the tailnet identity against the "ssh" rules, so a grant to tcp:22
alone gets a connection refused; whoever needs a shell also needs an ssh rule. tailscaled ends sessions
the new policy no longer allows ("Access revoked") at a slot change.

There is always a "grants" key, even when empty: a policy with neither
"acls" nor "grants" means ALLOW ALL in Headscale.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
TAG_RE = re.compile(r"^tag:[a-z0-9][a-z0-9-]{0,62}$")
SCHEDULER_TAG = "tag:scheduler"
SCHEDULER_USER = "labsched"  # the account the slot agent logs in as (setup-lab-pc.sh creates it)
ACCOUNT_RE = re.compile(r"^grp-[a-z0-9-]{1,28}$")
SSH_PORT = "tcp:22"  # Tailscale SSH: every ssh rule needs network access to this port as well


class PolicyError(ValueError):
    pass


@dataclass
class PolicyInput:
    admins: list[str]
    groups: dict[str, list[str]]  # slug -> member emails
    pc_tags: list[str]
    active: list[tuple[str, str]]  # (group slug, pc tag) pairs in slot now
    ports: list[str] = field(default_factory=lambda: ["tcp:22"])
    extra_grants: list[dict] = field(default_factory=list)
    extra_ssh: list[dict] = field(default_factory=list)  # admin-maintained Tailscale SSH rules
    accounts: dict[str, str] = field(default_factory=dict)  # slug -> the group's Linux account on its PC


def _tags_in(obj) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, str):
        if obj.startswith("tag:"):
            found.add(obj.split(":", 2)[0] + ":" + obj.split(":", 2)[1])
    elif isinstance(obj, list):
        for x in obj:
            found |= _tags_in(x)
    elif isinstance(obj, dict):
        for k in ("src", "dst"):
            if k in obj:
                found |= _tags_in(obj[k])
    return found


def build_policy(inp: PolicyInput, admins_only: bool = False) -> dict:
    """Return the policy document. admins_only drops every slot grant (fail closed)."""
    for slug in inp.groups:
        if not SLUG_RE.match(slug):
            raise PolicyError(f"bad group slug {slug!r}")
    for tag in inp.pc_tags:
        if not TAG_RE.match(tag):
            raise PolicyError(f"bad PC tag {tag!r}")
    for email in inp.admins + [e for m in inp.groups.values() for e in m]:
        if email.count("@") != 1:
            raise PolicyError(f"bad email {email!r}")

    groups: dict[str, list[str]] = {}
    admins = sorted(set(inp.admins))
    if admins:
        groups["group:admins"] = admins
    for slug, members in sorted(inp.groups.items()):
        if members:
            groups[f"group:{slug}"] = sorted(set(members))

    pc_tags = sorted(set(inp.pc_tags))
    grants: list[dict] = []
    ssh: list[dict] = []
    if admins and pc_tags:
        grants.append({"src": ["group:admins"], "dst": pc_tags, "ip": ["*"]})
        ssh.append({"action": "accept", "src": ["group:admins"], "dst": pc_tags, "users": ["autogroup:nonroot"]})
    if pc_tags:
        grants.append({"src": [SCHEDULER_TAG], "dst": pc_tags, "ip": [SSH_PORT]})
        ssh.append({"action": "accept", "src": [SCHEDULER_TAG], "dst": pc_tags, "users": [SCHEDULER_USER]})

    if not admins_only:
        by_group: dict[str, set[str]] = {}
        for slug, tag in inp.active:
            if f"group:{slug}" in groups and tag in pc_tags:
                by_group.setdefault(slug, set()).add(tag)
        ports = list(inp.ports) if SSH_PORT in inp.ports else [SSH_PORT, *inp.ports]
        for slug, tags in sorted(by_group.items()):
            grants.append({"src": [f"group:{slug}"], "dst": sorted(tags), "ip": ports})
            account = inp.accounts.get(slug, "")
            if ACCOUNT_RE.match(account):
                ssh.append({"action": "accept", "src": [f"group:{slug}"], "dst": sorted(tags), "users": [account]})

    for g in inp.extra_grants:
        if not isinstance(g, dict) or "src" not in g or "dst" not in g:
            raise PolicyError(f"extra grant needs src and dst: {g!r}")
        grants.append(g)
    for rule in inp.extra_ssh:
        if not isinstance(rule, dict) or not all(isinstance(rule.get(k), list) and rule[k]
                                                 for k in ("src", "dst", "users")):
            raise PolicyError(f"extra SSH rule needs src, dst and users lists: {rule!r}")
        if rule.get("action", "accept") != "accept":
            raise PolicyError(f"extra SSH rule action must be accept: {rule!r}")
        if "*" in rule["dst"]:
            raise PolicyError(f"extra SSH rule dst cannot be *: name the tags, or autogroup:self: {rule!r}")
        ssh.append({"action": "accept", **{k: v for k, v in rule.items() if k != "action"}})
        grants.append({"src": rule["src"], "dst": rule["dst"], "ip": [SSH_PORT]})  # the connection itself

    owner = ["group:admins"] if admins else []
    tags = set(pc_tags) | {SCHEDULER_TAG}
    for g in grants + ssh:
        tags |= _tags_in(g)
    policy: dict = {}
    if groups:
        policy["groups"] = groups
    if owner:
        policy["tagOwners"] = {t: owner for t in sorted(tags)}
    policy["grants"] = grants
    if ssh:
        policy["ssh"] = ssh
    return policy


def render(policy: dict, header: str) -> str:
    lines = "".join(f"// {line}\n" for line in header.splitlines())
    return lines + json.dumps(policy, indent=2) + "\n"
