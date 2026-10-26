"""Local demo of the admin desk with illustrative data. Development only, never deployed.

    env -u PYTHONPATH .venv/bin/python devserver.py   ->  http://127.0.0.1:8765/slots/dev-login?email=admin@students.example.edu
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "tests"))
import uvicorn  # noqa: E402
from conftest import Clock, FakeHeadscale, FakeSSH  # noqa: E402

from labslots.agent import Agent, Hub  # noqa: E402
from labslots.hub import add_server  # noqa: E402
from labslots.sheets import FakeSheets  # noqa: E402

from labslots.config import Config  # noqa: E402
from labslots.db import DB  # noqa: E402
from labslots.headscale import Node  # noqa: E402
from labslots.web import create_app  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="labslots-demo-"))
db = DB(tmp / "demo.db")
db.x("INSERT INTO admins(email, added_at, added_by) VALUES ('admin@students.example.edu', 'x', 'demo')")
db.x("INSERT INTO terms(name, start_date, end_date) VALUES ('Fall 2026 pilot', '2026-10-01', '2026-12-31')")
colors = ["#ffe08a", "#b8e6b0", "#f7c6d9", "#bcdcf5", "#ffd0a6", "#d9ccf2"]
gids = []
for i, letter in enumerate("abcdef"):
    gid = db.x("INSERT INTO groups(name, slug, linux_account, color, short) VALUES (?, ?, ?, ?, ?)",
               (f"Thesis {letter.upper()} (example)", f"thesis-{letter}", f"grp-thesis-{letter}", colors[i],
                f"TH-{letter.upper()}"))
    for n in range(5):
        db.x("INSERT INTO members(group_id, email) VALUES (?, ?)", (gid, f"{letter}{n + 1}.student@students.example.edu"))
    gids.append(gid)
pcs = []
for name, tag, ssh in (("pc-25", "tag:pc25", 1), ("pc-41", "tag:pc-41", 1), ("pc-43", "tag:pc-43", 0),
                       ("pc-44", "tag:pc-44", 0), ("pc-45", "tag:pc-45", 0)):
    pcs.append(db.x("INSERT INTO pcs(name, tag, ssh_enabled) VALUES (?, ?, ?)", (name, tag, ssh)))
# the lab's weekly table (A C E / B D F, Sunday maintenance), shifted per PC
day, night = [0, 2, 4, 0, 2, 4], [1, 3, 5, 1, 3, 5]
for k, pc in enumerate(pcs):
    for wd in range(6):
        for half, seq in (("day", day), ("night", night)):
            g = gids[(seq[wd] + k) % 6]
            status = "approved"
            if k == 4 and wd < 3:
                continue  # pc-45 half empty: newly added
            db.x("INSERT INTO bookings(term_id, pc_id, group_id, weekday, half, status, requested_by, requested_at, note) "
                 "VALUES (1, ?, ?, ?, ?, ?, 'admin@students.example.edu', '2026-10-15T10:00:00', ?)",
                 (pc, g, wd, half, status, "agreed in lab meeting" if status == "pending" else ""))
db.x("INSERT INTO pc_state(pc_id, active_account, updated_at, last_error) VALUES (?, 'grp-thesis-e', 'x', NULL)", (pcs[1],))
db.x("INSERT INTO pc_state(pc_id, active_account, updated_at, last_error) VALUES (?, 'grp-thesis-d', 'x', "
     "'pc-25: switch to grp-thesis-a failed: ssh: connect to host 100.64.0.5 port 22: Connection timed out')", (pcs[0],))
for i, (a, d) in enumerate((("booking-approved", "#12 Thesis C (example) on pc-41, Tue night"),
                            ("policy-applied", "Sat 17 Oct 06:00-17:55: Thesis E (example)->pc-25, Thesis F (example)->pc-41"),
                            ("pc-switch-error", "pc-25: switch to grp-thesis-a failed: connection timed out"),
                            ("login", ""))):
    db.audit("admin@students.example.edu" if a != "policy-applied" and a != "pc-switch-error" else "scheduler", a, d)
# enrollments (illustrative): two waiting, one enrolled for the demo student
def _enroll(by, name, title, sup, hw, members, status="pending", gid=None):
    eid = db.x("INSERT INTO enrolments(submitted_by, submitted_at, group_name, thesis_title, supervisor, co_supervisor, "
               "hardware, status, group_id, decided_by, decided_at) VALUES (?, '2026-10-16T21:14:00', ?, ?, ?, '', ?, ?, ?, ?, ?)",
               (by, name, title, sup, hw, status, gid, "admin@students.example.edu" if status != "pending" else None,
                "2026-10-17T11:02:00" if status != "pending" else None))
    for i, (n, sid, em) in enumerate(members):
        db.x("INSERT INTO enrolment_members VALUES (?, ?, ?, ?, ?)", (eid, i, n, sid, em))
_enroll("nusrat.j@students.example.edu", "Speech ASR (example)", "Low-resource speech recognition", "Dr. Example Supervisor",
       "One GPU with 12 GB or more for training; about 200 GB of disk for audio datasets.",
       [("Nusrat Jahan", "21101231", "nusrat.j@students.example.edu"), ("Tanvir Hasan", "21101245", "tanvir.h@students.example.edu"),
        ("Farhan Kabir", "21101302", "farhan.k@students.example.edu")])
_enroll("arif.r@students.example.edu", "Road Scene Vision (example)", "Lane detection on city traffic video", "Dr. Example Supervisor",
       "GPU for inference only; 50 GB disk.", [("Arif Rahman", "21201011", "arif.r@students.example.edu")])
_enroll("a1.student@students.example.edu", "Thesis A (example)", "Example thesis title", "Dr. Example Supervisor", "1 GPU",
       [("A One", "21100001", "a1.student@students.example.edu"), ("A Two", "21100002", "a2.student@students.example.edu")],
       status="approved", gid=gids[0])
nodes = [Node(id="5", name="pc25", owner="", tags=["tag:pc25"], online=True, ips=["100.64.0.5"]),
         Node(id="2", name="pc-41", owner="", tags=["tag:pc-41"], online=True, ips=["100.64.0.2"]),
         Node(id="3", name="pc-43", owner="", tags=["tag:pc-43"], online=False, ips=["100.64.0.3"]),
         Node(id="9", name="pc-44", owner="", tags=["tag:pc-44"], online=True, ips=["100.64.0.9"])]
hs = FakeHeadscale(nodes)
clock = Clock(datetime(2026, 10, 17, 16, 0))
cfg = Config(db_path=str(tmp / "demo.db"), base_path="/slots", public_url="http://127.0.0.1:8765",
             oidc_issuer="", oidc_client_id="", oidc_client_secret="",
             session_secret="demo-only", bootstrap_admins=[], interval=30, dev_login=True, run_scheduler=False)
app = create_app(cfg, db=db, clock=clock, sheet=FakeSheets())
app.state.sync.pull()
# one slot agent, talking to the hub in-process, so the Now and PCs pages have live-looking reports
from fastapi.testclient import TestClient  # noqa: E402
agent = Agent(DB(tmp / "agent.db"), Hub("http://testserver/slots", add_server(db, "hs1"), http=TestClient(app)),
              hs, FakeSSH(), clock)
agent.run_once()
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
