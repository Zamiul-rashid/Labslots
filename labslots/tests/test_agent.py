import json

import httpx
from conftest import MON_10, MON_21, Clock, FakeHeadscale, FakeSSH, nodes
from test_web import client

from labslots.agent import Agent, Hub
from labslots.db import DB
from labslots.hub import add_server, server_slice


def hub_and_agent(tmp_path, server="hs1"):
    c, db, sheet = client(tmp_path)
    token = add_server(db, server)
    hs, ssh = FakeHeadscale(nodes()), FakeSSH()
    clock = Clock(MON_10)
    agent = Agent(DB(tmp_path / f"agent-{server}.db"), Hub("http://testserver/slots", token, http=c), hs, ssh, clock)
    return c, db, agent, hs, ssh, clock


def test_agent_enforces_its_slice_and_reports(tmp_path):
    c, db, agent, hs, ssh, clock = hub_and_agent(tmp_path)
    db.set_setting("ssh_enforce", "1")
    rep = agent.run_once()
    assert not rep.errors, rep.errors
    assert '"group:thesis-a"' in hs.policies[-1] and "tag:pc-41" in hs.policies[-1]
    assert ssh.calls == [("100.64.0.2", "grp-thesis-a")]
    c.get("/slots/dev-login?email=boss@example.edu")
    now = c.get("/slots/").text
    assert "grp-thesis-a" in now and "100.64.0.2" in now and "No report" not in now
    clock.dt = MON_21                                     # next slot: thesis-b on pc-41
    agent.run_once()
    assert '"group:thesis-b"' in hs.policies[-1] and ssh.calls[-1][1] == "grp-thesis-b"


def test_agent_keeps_enforcing_when_the_hub_is_down(tmp_path):
    c, db, agent, hs, ssh, clock = hub_and_agent(tmp_path)
    agent.run_once()
    n = len(hs.policies)

    def down(req):
        raise httpx.ConnectError("hub down")
    agent.hub.http = httpx.Client(transport=httpx.MockTransport(down))
    clock.dt = MON_21
    rep = agent.run_once()                                 # no hub, still switches to the night slot
    assert len(hs.policies) == n + 1 and '"group:thesis-b"' in hs.policies[-1]
    assert agent.db.one("SELECT 1 FROM audit WHERE action = 'hub-unreachable'")
    assert rep.policy_applied


def test_each_server_gets_only_its_pcs(tmp_path):
    c, db, _ = client(tmp_path)
    add_server(db, "hs1")
    add_server(db, "hs2")
    db.x("UPDATE pcs SET server = 'hs2' WHERE name = 'pc-43'")
    db.set_setting("test_rotation", json.dumps({"pc_id": 2, "groups": [1], "seconds": 60,
                                                "start": "2026-10-19T10:00:00", "end": "2026-10-19T10:05:00"}))
    one, two = server_slice(db, "hs1"), server_slice(db, "hs2")
    assert [p["name"] for p in one["pcs"]] == ["pc-41"] and [p["name"] for p in two["pcs"]] == ["pc-43"]
    assert one["settings"]["test_rotation"] == "" and two["settings"]["test_rotation"]   # rotation goes to pc-43's server
    assert {g["slug"] for g in two["groups"]} == {"thesis-a"}                           # the rotation's group


def test_reapply_now_forces_a_rewrite(tmp_path):
    c, db, agent, hs, ssh, clock = hub_and_agent(tmp_path)
    agent.run_once()
    agent.run_once()
    n = len(hs.policies)                                   # unchanged policy is not rewritten
    c.get("/slots/dev-login?email=boss@example.edu")
    c.post("/slots/enforce", headers={"origin": "http://testserver"})
    agent.run_once()
    assert len(hs.policies) == n + 1
    agent.run_once()
    assert len(hs.policies) == n + 1                       # only once per request


def test_pc_state_survives_slice_updates(tmp_path):
    c, db, agent, hs, ssh, clock = hub_and_agent(tmp_path)
    db.set_setting("ssh_enforce", "1")
    agent.run_once()
    db.x("UPDATE groups SET name = 'Renamed' WHERE slug = 'thesis-b'")   # any change to the slice
    agent.run_once()
    assert len(ssh.calls) == 1                             # pc-41 is not switched again for nothing


def test_agent_without_any_schedule_leaves_headscale_alone(tmp_path):
    c, db, agent, hs, ssh, clock = hub_and_agent(tmp_path)

    def down(req):
        raise httpx.ConnectError("hub not up yet")
    agent.hub.http = httpx.Client(transport=httpx.MockTransport(down))
    rep = agent.run_once()                                 # first start, hub unreachable
    assert hs.policies == [] and hs.deleted == [] and ssh.calls == []
    assert "waiting for the first schedule" in rep.errors[0]
    agent.hub.http = c
    agent.run_once()
    assert '"group:thesis-a"' in hs.policies[-1]


def test_group_removed_from_the_sheet_loses_its_devices_a_day_later(tmp_path):
    from datetime import timedelta
    c, db, agent, hs, ssh, clock = hub_and_agent(tmp_path)
    agent.run_once()
    db.x("DELETE FROM groups WHERE slug = 'thesis-b'")     # end of term: the row is gone from the Sheet
    agent.run_once()
    assert agent.db.one("SELECT email FROM former_members")["email"] == "bob@students.example.edu"
    assert "20" not in hs.deleted                          # a day's grace, in case it was a mistake
    clock.dt += timedelta(hours=23)
    agent.run_once()
    assert "20" not in hs.deleted
    clock.dt += timedelta(hours=2)
    agent.run_once()
    assert "20" in hs.deleted and agent.db.one("SELECT 1 FROM audit WHERE action = 'device-removed'")
    assert not {"2", "3", "10", "30", "40"} & set(hs.deleted)   # PCs, students still in a group, admins, strangers


def test_restored_group_is_not_cleaned_up(tmp_path):
    from datetime import timedelta
    c, db, agent, hs, ssh, clock = hub_and_agent(tmp_path)
    agent.run_once()
    gid = db.one("SELECT id FROM groups WHERE slug = 'thesis-b'")["id"]
    db.x("DELETE FROM members WHERE group_id = ?", (gid,))  # members cleared by mistake...
    agent.run_once()
    db.x("INSERT INTO members(group_id, email) VALUES (?, 'bob@students.example.edu')", (gid,))   # ...and put back
    agent.run_once()
    clock.dt += timedelta(days=2)
    agent.run_once()
    assert agent.db.one("SELECT 1 FROM former_members") is None and "20" not in hs.deleted
