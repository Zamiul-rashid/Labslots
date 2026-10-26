"""Slot agent: enforces the schedule on one Headscale server.

Runs next to its Headscale (one agent per server). Every few seconds it asks the
hub (the website) for this server's slice of the schedule, stores it in its own
SQLite file, runs the Enforcer against its Headscale and lab PCs, and posts a
report back for the website's Now and PCs pages. If the hub cannot be reached it
keeps enforcing the last slice it stored, so the website can be down, restarted
or redeployed without touching access.

    python -m labslots.agent
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from .db import DB, now_iso
from .enforcer import Enforcer, Report
from .headscale import Headscale
from .hub import apply_slice
from .slots import slot_at
from .sshexec import SSH

log = logging.getLogger("labslots.agent")


class Hub:
    def __init__(self, url: str, token: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.http = http or httpx.Client(timeout=15)
        self.headers = {"Authorization": f"Bearer {token}"}

    def state(self) -> dict:
        r = self.http.get(f"{self.url}/api/agent/state", headers=self.headers)
        r.raise_for_status()
        return r.json()

    def report(self, body: dict) -> None:
        r = self.http.post(f"{self.url}/api/agent/report", content=json.dumps(body, default=str),
                           headers=self.headers | {"Content-Type": "application/json"})
        r.raise_for_status()


class Agent:
    def __init__(self, db: DB, hub: Hub, hs: Headscale | None, ssh: SSH | None, clock=datetime.now):
        self.db, self.hub, self.hs, self.ssh = db, hub, hs, ssh
        self.enforcer = Enforcer(db, hs, ssh, clock)
        self.hub_error = ""

    def fetch(self) -> bool:
        try:
            s = self.hub.state()
        except Exception as e:  # noqa: BLE001
            if not self.hub_error:
                self.db.audit("agent", "hub-unreachable", str(e)[:300])
            self.hub_error = str(e)[:300]
            return False
        if s.get("version") != self.db.setting("slice_version"):
            apply_slice(self.db, s, self.enforcer.clock())
            self.db.set_setting("slice_version", s.get("version", ""))
        self.db.set_setting("slice_at", now_iso())
        self.db.set_setting("hub_force", s.get("force", "0"))
        if self.hub_error:
            self.db.audit("agent", "hub-reachable", "")
        self.hub_error = ""
        return True

    def run_once(self) -> Report:
        self.fetch()
        if not self.db.setting("slice_version"):
            # Never had a schedule (first start, hub not up yet): an empty copy would lock out every
            # group and every admin, so leave Headscale exactly as it is until the first fetch.
            rep = Report(slot=slot_at(self.enforcer.clock()),
                         errors=[f"waiting for the first schedule from the hub: {self.hub_error}"])
            try:
                self.hub.report(self.report_body(rep))
            except Exception:  # noqa: BLE001
                pass
            return rep
        force = self.db.setting("hub_force") != self.db.setting("done_force")
        t0 = time.monotonic()
        rep = self.enforcer.reconcile(force=force)
        self.last_seconds = round(time.monotonic() - t0, 2)
        if force:
            self.db.set_setting("done_force", self.db.setting("hub_force"))
        try:
            self.hub.report(self.report_body(rep))
        except Exception as e:  # noqa: BLE001
            log.warning("could not report to the hub: %s", e)
        return rep

    def report_body(self, rep: Report) -> dict:
        nodes, live, node_error = [], "", ""
        if self.hs is not None:
            try:
                nodes = [{"name": n.name, "tags": n.tags, "online": n.online, "ipv4": n.ipv4}
                         for n in self.hs.nodes() if n.tags]
            except Exception as e:  # noqa: BLE001
                node_error = str(e)[:300]
            try:
                live = self.hs.get_policy()
            except Exception as e:  # noqa: BLE001
                live = f"// could not read the live policy: {e}"
        rot = rep.rotation
        return {
            "at": now_iso(), "slot": rep.slot.label,
            "policy_applied": rep.policy_applied, "policy_changed": rep.policy_changed,
            "admins_only": rep.admins_only, "errors": rep.errors, "deleted": rep.deleted,
            "would_delete": rep.would_delete, "ssh": rep.ssh,
            "rotation": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in rot.items()} if rot else None,
            "nodes": nodes, "node_error": node_error, "live_policy": live,
            "applied_at": self.db.setting("applied_policy_at"),
            "pc_state": {r["pc_id"]: {"active_account": r["active_account"], "updated_at": r["updated_at"],
                                      "last_error": r["last_error"]}
                         for r in self.db.q("SELECT * FROM pc_state")},
            "run_seconds": getattr(self, "last_seconds", None), "ssh_seconds": rep.ssh_seconds,
            "hub_error": self.hub_error, "slice_at": self.db.setting("slice_at"),
            "slice_version": self.db.setting("slice_version"),
        }


def _read(path: str) -> str:
    return Path(path).read_text().strip() if path and Path(path).exists() else ""


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    e = os.environ.get
    data = e("LABSLOTS_DATA", "/data")
    db = DB(e("AGENT_DB", f"{data}/agent.db"))
    hub = Hub(e("AGENT_HUB_URL", "http://127.0.0.1:3100/slots"), _read(e("AGENT_TOKEN_FILE", "/run/secrets/agent_token")))
    key = _read(e("HEADSCALE_API_KEY_FILE", "/run/secrets/headscale_api_key"))
    hs = Headscale(e("HEADSCALE_URL", "http://127.0.0.1:8080"), key) if key else None
    ssh = SSH(e("LABSLOTS_SSH_KNOWN_HOSTS", f"{data}/ssh/known_hosts"), e("LABSLOTS_SSH_USER", "labsched"),
              e("LABSLOTS_SSH_SOCKS", ""))
    agent = Agent(db, hub, hs, ssh)
    interval = float(e("AGENT_INTERVAL", "15"))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    log.info("slot agent started: hub %s, headscale %s", hub.url, "configured" if hs else "NOT configured")
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            rep = agent.run_once()
            if rep.errors:
                log.warning("reconcile errors: %s", rep.errors)
        except Exception:  # noqa: BLE001
            log.exception("agent run crashed")
        try:
            Path(data, "heartbeat").touch()  # for the container healthcheck
        except OSError:
            pass
        wait = 5 if db.setting("test_rotation") else interval  # quick checks while a test rotation runs
        stop.wait(max(1.0, wait - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
