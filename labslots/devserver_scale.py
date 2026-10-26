"""Load-looking demo: 70 PCs, 57 thesis groups, ~90% of weekday slots booked. Development only.

    env -u PYTHONPATH .venv/bin/python devserver_scale.py  ->  http://127.0.0.1:8766/slots/dev-login?email=admin@students.example.edu
"""

import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "tests"))
import uvicorn  # noqa: E402
from conftest import Clock, FakeHeadscale, FakeSSH  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from labslots.agent import Agent, Hub  # noqa: E402
from labslots.config import Config  # noqa: E402
from labslots.db import DB  # noqa: E402
from labslots.headscale import Node  # noqa: E402
from labslots.hub import add_server  # noqa: E402
from labslots.sheets import FakeSheets  # noqa: E402
from labslots.sync import next_color  # noqa: E402
from labslots.web import create_app  # noqa: E402

rnd = random.Random(7)
tmp = Path(tempfile.mkdtemp(prefix="labslots-scale-"))
db = DB(tmp / "demo.db")
db.x("INSERT INTO admins(email, added_at, added_by) VALUES ('admin@students.example.edu', 'x', 'demo')")
db.x("INSERT INTO terms(name, start_date, end_date) VALUES ('Fall 2026', '2026-10-01', '2027-01-31')")
topics = ["ASR", "NLP", "CV", "ROB", "MED", "AGRI", "GEO", "SEC", "IOT", "RL", "GAN", "BIO", "LLM", "OCR", "TS"]
gids, colors = [], []
for i in range(57):
    short = f"{topics[i % len(topics)]}-{i // len(topics) + 1}"
    slug = short.lower()
    color = next_color(colors)
    colors.append(color)
    gid = db.x("INSERT INTO groups(name, slug, linux_account, color, short) VALUES (?, ?, ?, ?, ?)",
               (f"{short} thesis group (example)", slug, f"grp-{slug}", color, short))
    for n in range(rnd.randint(2, 4)):
        db.x("INSERT INTO members(group_id, email) VALUES (?, ?)", (gid, f"{slug}.{n + 1}@students.example.edu"))
    gids.append(gid)
pcs = [db.x("INSERT INTO pcs(name, tag, ssh_enabled) VALUES (?, ?, 1)", (f"pc-{k}", f"tag:pc-{k}")) for k in range(1, 71)]
# Mon-Sat both halves (Sunday is maintenance), ~90% booked, each group on a few PCs
for k, pc in enumerate(pcs):
    for wd in range(6):
        for h, half in enumerate(("day", "night")):
            if rnd.random() < 0.10:
                continue
            g = gids[(k * 3 + wd * 7 + h * 29) % len(gids)]
            db.x("INSERT INTO bookings(term_id, pc_id, group_id, weekday, half, status, requested_by, requested_at) "
                 "VALUES (1, ?, ?, ?, ?, 'approved', 'demo', 'x')", (pc, g, wd, half))
nodes = [Node(id=str(100 + k), name=f"pc-{k}", owner="", tags=[f"tag:pc-{k}"], online=rnd.random() > 0.06,
              ips=[f"100.64.1.{k}"]) for k in range(1, 71)]
clock = Clock(datetime(2026, 10, 22, 14, 0))
cfg = Config(db_path=str(tmp / "demo.db"), base_path="/slots", public_url="http://127.0.0.1:8766",
             oidc_issuer="", oidc_client_id="", oidc_client_secret="",
             session_secret="demo-only", bootstrap_admins=[], interval=30, dev_login=True, run_scheduler=False)
app = create_app(cfg, db=db, clock=clock, sheet=FakeSheets())
app.state.sync.pull()
db.set_setting("ssh_enforce", "1")


class FlakySSH(FakeSSH):
    def switch(self, host, account, keys=""):
        if host.endswith(".17") or host.endswith(".52"):
            from labslots.sshexec import SSHResult
            return SSHResult(False, "ssh: connect to host port 22: Connection timed out")
        return super().switch(host, account, keys)


agent = Agent(DB(tmp / "agent.db"), Hub("http://testserver/slots", add_server(db, "hs1"), http=TestClient(app)),
              FakeHeadscale(nodes), FlakySSH(), clock)
agent.run_once()
db.x("UPDATE servers SET last_seen = ?", (clock().isoformat(timespec="seconds"),))  # the demo's clock is frozen
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")
