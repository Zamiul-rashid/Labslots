import json

from conftest import MON_10, MON_21, TUE_03, Clock, FakeHeadscale, FakeSSH, nodes, seed

from labslots.enforcer import Enforcer


def make(db, when, hs=None, ssh=None):
    return Enforcer(db, hs or FakeHeadscale(nodes()), ssh or FakeSSH(), Clock(when))


def last_policy(hs):
    """Parse the newest policy written, skipping its // header lines."""
    return json.loads("\n".join(l for l in hs.policies[-1].splitlines() if not l.startswith("//")))


def test_day_slot_grants_group_a_only(db):
    seed(db)
    hs = FakeHeadscale(nodes())
    rep = make(db, MON_10, hs).reconcile()
    p = last_policy(hs)
    srcs = [g["src"] for g in p["grants"]]
    assert ["group:thesis-a"] in srcs and ["group:thesis-b"] not in srcs
    assert rep.policy_changed and not rep.errors


def test_night_after_midnight_still_monday_night(db):
    seed(db)
    hs = FakeHeadscale(nodes())
    make(db, TUE_03, hs).reconcile()
    srcs = [g["src"] for g in last_policy(hs)["grants"]]
    assert ["group:thesis-b"] in srcs and ["group:thesis-a"] not in srcs


def test_switch_window_closes_the_slot_before_the_next_group(db):
    """17:55-18:00: the day group's grant and account are gone, the night group is not in yet."""
    from datetime import datetime
    seed(db)
    db.set_setting("ssh_enforce", "1")
    hs, ssh = FakeHeadscale(nodes()), FakeSSH()
    e = make(db, datetime(2026, 10, 19, 17, 50), hs, ssh)
    e.reconcile()
    assert ssh.calls[-1] == ("100.64.0.2", "grp-thesis-a")
    assert e.next_change() == datetime(2026, 10, 19, 17, 55)
    e.clock.dt = datetime(2026, 10, 19, 17, 56)
    rep = e.reconcile()
    srcs = [g["src"] for g in last_policy(hs)["grants"]]
    assert ["group:thesis-a"] not in srcs and ["group:thesis-b"] not in srcs and ["group:admins"] in srcs
    assert last_policy(hs)["ssh"] and not any(r["src"][0].startswith("group:thesis") for r in last_policy(hs)["ssh"])
    assert ssh.calls[-1] == ("100.64.0.2", None) and rep.switching and not rep.admins_only
    assert "switch-over" in hs.policies[-1].splitlines()[0]
    assert e.next_change() == datetime(2026, 10, 19, 18, 0)
    e.clock.dt = datetime(2026, 10, 19, 18, 0)
    e.reconcile()
    assert ["group:thesis-b"] in [g["src"] for g in last_policy(hs)["grants"]]
    assert ssh.calls[-1] == ("100.64.0.2", "grp-thesis-b")


def test_pending_booking_has_no_effect(db):
    seed(db)
    hs = FakeHeadscale(nodes())
    from datetime import datetime
    make(db, datetime(2026, 10, 20, 10), hs).reconcile()  # Tuesday day: only a pending booking
    assert not any(g["src"][0].startswith("group:thesis") for g in last_policy(hs)["grants"])


def test_policy_not_rewritten_when_unchanged(db):
    seed(db)
    hs = FakeHeadscale(nodes())
    e = make(db, MON_10, hs)
    e.reconcile()
    e.reconcile()
    assert len(hs.policies) == 1
    e.reconcile(force=True)
    assert len(hs.policies) == 2


def test_fail_closed_when_set_policy_rejected(db):
    seed(db)
    hs = FakeHeadscale(nodes(), fail_set=1)
    rep = make(db, MON_10, hs).reconcile()
    assert rep.admins_only and rep.errors
    srcs = [g["src"] for g in last_policy(hs)["grants"]]
    assert ["group:thesis-a"] not in srcs and ["group:admins"] in srcs
    assert db.setting("applied_policy_sha") == ""  # retried next run


def test_cleanup_dry_run_then_real(db):
    seed(db)
    hs = FakeHeadscale(nodes())
    rep = make(db, MON_10, hs).reconcile()
    # Monday day: A in slot. Bob (B) is out of slot -> his device; alice has 3 devices, cap 2 -> oldest.
    assert sorted(rep.would_delete) == ["alice-old (alice@students.example.edu, id 12)", "bob-laptop (bob@students.example.edu, id 20)"]
    assert hs.deleted == []
    db.set_setting("device_cleanup", "1")
    rep = make(db, MON_10, hs).reconcile()
    assert sorted(hs.deleted) == ["12", "20"]
    # admins, tagged PCs and people not on any roster are never touched
    assert not {"2", "3", "30", "40"} & set(hs.deleted)


def test_ssh_switch_only_when_enabled_and_idempotent(db):
    seed(db)
    ssh = FakeSSH()
    e = make(db, MON_10, ssh=ssh)
    e.reconcile()
    assert ssh.calls == []  # global switch off
    db.set_setting("ssh_enforce", "1")
    e.reconcile()
    assert ssh.calls == [("100.64.0.2", "grp-thesis-a")]  # pc-43 has switching off
    e.reconcile()
    assert len(ssh.calls) == 1  # already in desired state
    e.clock.dt = MON_21
    e.reconcile()
    assert ssh.calls[-1] == ("100.64.0.2", "grp-thesis-b")


def test_ssh_failure_is_retried(db):
    seed(db)
    db.set_setting("ssh_enforce", "1")
    ssh = FakeSSH(ok=False)
    e = make(db, MON_10, ssh=ssh)
    rep = e.reconcile()
    assert rep.errors and db.one("SELECT last_error FROM pc_state")["last_error"]
    ssh.ok = True
    e.reconcile()
    assert len(ssh.calls) == 2 and db.one("SELECT last_error FROM pc_state")["last_error"] is None


def test_free_slot_switches_pc_to_nobody(db):
    from datetime import datetime
    seed(db)
    db.set_setting("ssh_enforce", "1")
    ssh = FakeSSH()
    make(db, datetime(2026, 10, 21, 10), ssh=ssh).reconcile()  # Wednesday: no bookings
    assert ssh.calls == [("100.64.0.2", None)]


def test_test_rotation_cycles_groups_on_one_pc(db):
    import json as _j
    from datetime import timedelta
    ids = seed(db)
    db.set_setting("test_rotation", _j.dumps({"pc_id": ids["p2"], "groups": [ids["a"], ids["b"]], "seconds": 60,
                                              "start": MON_10.isoformat(),
                                              "end": (MON_10 + timedelta(minutes=3)).isoformat()}))
    hs = FakeHeadscale(nodes())
    e = make(db, MON_10 + timedelta(seconds=10), hs)
    rep = e.reconcile()
    assert rep.rotation and {(b["group_name"], b["pc_name"]) for b in rep.active} == {("Thesis A", "pc-41"), ("Thesis A", "pc-43")}
    e.clock.dt = MON_10 + timedelta(seconds=70)          # second slot: B on pc-43, A keeps its real pc-41 booking
    rep = e.reconcile()
    assert ("Thesis B", "pc-43") in {(b["group_name"], b["pc_name"]) for b in rep.active}
    assert e.next_change() == MON_10 + timedelta(seconds=120)
    e.clock.dt = MON_10 + timedelta(minutes=4)            # rotation over: normal routine only
    rep = e.reconcile()
    assert rep.rotation is None and {b["pc_name"] for b in rep.active} == {"pc-41"}


def test_pcs_are_switched_in_parallel(db):
    import threading
    import time

    from conftest import MON_10, Clock, FakeHeadscale, nodes, seed

    from labslots.enforcer import Enforcer
    from labslots.headscale import Node
    from labslots.sshexec import SSHResult
    seed(db)
    db.set_setting("ssh_enforce", "1")
    ns = nodes()
    for k in range(3, 23):   # 20 more PCs, all ssh-enabled
        db.x("INSERT INTO pcs(name, tag, ssh_enabled) VALUES (?, ?, 1)", (f"pc-x{k}", f"tag:pc-x{k}"))
        ns.append(Node(id=str(500 + k), name=f"pc-x{k}", owner="", tags=[f"tag:pc-x{k}"], online=True,
                       ips=[f"100.64.2.{k}"]))

    class SlowSSH:
        def __init__(self):
            self.live, self.peak, self.lock = 0, 0, threading.Lock()

        def switch(self, host, account, keys=""):
            with self.lock:
                self.live += 1
                self.peak = max(self.peak, self.live)
            time.sleep(0.2)
            with self.lock:
                self.live -= 1
            return SSHResult(True, "ok")
    ssh = SlowSSH()
    rep = Enforcer(db, FakeHeadscale(ns), ssh, Clock(MON_10)).reconcile()
    assert len(rep.ssh) == 21 and not rep.errors       # pc-41 plus the 20 new ones
    assert ssh.peak == 10 and rep.ssh_seconds < 1.5     # 21 x 0.2 s one by one would take 4.2 s


def test_devices_of_students_in_no_group_go_after_a_day(db):
    from labslots.headscale import Node
    seed(db)
    db.x("INSERT INTO former_members(email, since) VALUES ('carol@students.example.edu', '2026-10-18T09:00:00')")  # 25 h
    db.x("INSERT INTO former_members(email, since) VALUES ('dan@students.example.edu', '2026-10-19T09:00:00')")    # 1 h
    db.x("INSERT INTO former_members(email, since) VALUES ('boss@example.edu', '2026-10-01T09:00:00')")     # an admin
    hs = FakeHeadscale(nodes() + [Node(id="50", name="carol-laptop", owner="carol@students.example.edu"),
                                  Node(id="51", name="dan-laptop", owner="dan@students.example.edu")])
    rep = make(db, MON_10, hs).reconcile()                 # device_cleanup is off: slot-end removals stay dry-run
    assert hs.deleted == ["50"] and rep.deleted == ["carol-laptop (carol@students.example.edu, id 50, in no group)"]
    assert "bob-laptop (bob@students.example.edu, id 20)" in rep.would_delete
    db.set_setting("former_cleanup", "0")
    hs.deleted.clear()
    rep = make(db, MON_10, hs).reconcile()
    assert hs.deleted == [] and "carol-laptop (carol@students.example.edu, id 50, in no group)" in rep.would_delete


def test_leavers_are_removed_a_batch_per_run(db):
    from labslots.enforcer import FORMER_PER_RUN
    from labslots.headscale import Node
    seed(db)
    leavers = [Node(id=str(100 + i), name=f"old-{i}", owner=f"old{i}@students.example.edu") for i in range(FORMER_PER_RUN + 5)]
    for n in leavers:
        db.x("INSERT INTO former_members(email, since) VALUES (?, '2026-10-01T09:00:00')", (n.owner,))
    hs = FakeHeadscale(nodes() + leavers)
    make(db, MON_10, hs).reconcile()
    assert len(hs.deleted) == FORMER_PER_RUN               # the rest wait for the next run, 15 s later
    make(db, MON_10, hs).reconcile()
    assert len(hs.deleted) == len(leavers)
