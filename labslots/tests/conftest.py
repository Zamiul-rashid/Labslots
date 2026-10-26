import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from labslots.db import DB  # noqa: E402
from labslots.headscale import Node  # noqa: E402
from labslots.sshexec import SSHResult  # noqa: E402


class FakeHeadscale:
    def __init__(self, nodes=None, fail_set=0):
        self._nodes = list(nodes or [])
        self.policies: list[str] = []
        self.deleted: list[str] = []
        self.fail_set = fail_set  # number of set_policy calls to fail first

    def nodes(self):
        return [n for n in self._nodes if n.id not in self.deleted]

    def set_policy(self, text):
        if self.fail_set > 0:
            self.fail_set -= 1
            raise RuntimeError("HTTP 400 policy rejected")
        self.policies.append(text)

    def get_policy(self):
        return self.policies[-1] if self.policies else ""

    def delete_node(self, node_id):
        self.deleted.append(node_id)


class FakeSSH:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def switch(self, host, account):
        self.calls.append((host, account))
        return SSHResult(self.ok, "slot-active=%s" % (account or "-"))


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def seed(db):
    """Two groups, two PCs, a term covering Oct 2026, Monday bookings."""
    db.x("INSERT OR IGNORE INTO admins(email, added_at, added_by) VALUES ('boss@example.edu', 'x', 'test')")
    db.x("INSERT INTO terms(name, start_date, end_date) VALUES ('T', '2026-10-01', '2026-12-31')")
    a = db.x("INSERT INTO groups(name, slug, linux_account, color) VALUES "
             "('Thesis A', 'thesis-a', 'grp-thesis-a', '#ffcccc')")
    b = db.x("INSERT INTO groups(name, slug, linux_account) VALUES ('Thesis B', 'thesis-b', 'grp-thesis-b')")
    db.x("INSERT INTO members(group_id, email) VALUES (?, 'alice@students.example.edu')", (a,))
    db.x("INSERT INTO members(group_id, email) VALUES (?, 'bob@students.example.edu')", (b,))
    p1 = db.x("INSERT INTO pcs(name, tag, ssh_enabled) VALUES ('pc-41', 'tag:pc-41', 1)")
    p2 = db.x("INSERT INTO pcs(name, tag, ssh_enabled) VALUES ('pc-43', 'tag:pc-43', 0)")
    # Monday day: A on pc-41. Monday night: B on pc-41. Tuesday day: B pending on pc-43.
    for g, p, wd, half, st in ((a, p1, 0, "day", "approved"), (b, p1, 0, "night", "approved"),
                               (b, p2, 1, "day", "pending")):
        db.x("INSERT INTO bookings(term_id, pc_id, group_id, weekday, half, status, requested_by, requested_at) "
             "VALUES (1, ?, ?, ?, ?, ?, 'test', 'x')", (p, g, wd, half, st))
    return {"a": a, "b": b, "p1": p1, "p2": p2}


def nodes():
    return [
        Node(id="2", name="pc-41", owner="", tags=["tag:pc-41"], online=True, ips=["100.64.0.2"]),
        Node(id="3", name="pc-43", owner="", tags=["tag:pc-43"], online=True, ips=["100.64.0.3"]),
        Node(id="10", name="alice-laptop", owner="alice@students.example.edu", ips=["100.64.0.10"], last_seen="3"),
        Node(id="11", name="alice-phone", owner="alice@students.example.edu", ips=["100.64.0.11"], last_seen="2"),
        Node(id="12", name="alice-old", owner="alice@students.example.edu", ips=["100.64.0.12"], last_seen="1"),
        Node(id="20", name="bob-laptop", owner="bob@students.example.edu", ips=["100.64.0.20"]),
        Node(id="30", name="boss-laptop", owner="boss@example.edu", ips=["100.64.0.30"]),
        Node(id="40", name="stranger", owner="eve@students.example.edu", ips=["100.64.0.40"]),
    ]


MON_10 = datetime(2026, 10, 19, 10, 0)   # Monday 19 Oct 2026, day slot
MON_21 = datetime(2026, 10, 19, 21, 0)   # Monday night slot
TUE_03 = datetime(2026, 10, 20, 3, 0)    # still Monday's night slot
