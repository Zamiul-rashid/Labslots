import json

from conftest import MON_10, Clock, seed
from fastapi.testclient import TestClient

from labslots.config import Config
from labslots.hub import add_server, store_report
from labslots.sheets import FakeSheets
from labslots.sync import GROUPS, routine_tab
from labslots.web import create_app

ORIGIN = {"origin": "http://testserver"}


def cfg(tmp_path):
    return Config(db_path=str(tmp_path / "w.db"), base_path="/slots", public_url="http://testserver",
                  oidc_issuer="", oidc_client_id="", oidc_client_secret="", session_secret="test-secret",
                  bootstrap_admins=["boss@example.edu"], interval=30, dev_login=True, run_scheduler=False)


def client(tmp_path, seeded=True):
    """The hub with a seeded schedule already pulled from a fake Sheet."""
    from labslots.db import DB
    db = DB(tmp_path / "w.db")
    if seeded:
        seed(db)
    sheet = FakeSheets()
    app = create_app(cfg(tmp_path), db=db, clock=Clock(MON_10), sheet=sheet)
    app.state.sync.pull()
    return TestClient(app), db, sheet


def test_healthz_reports_the_running_version(tmp_path):
    from importlib.metadata import version
    c, _, _ = client(tmp_path)
    r = c.get("/slots/healthz")
    assert r.status_code == 200 and r.json()["version"] == version("labslots")


def test_requires_admin(tmp_path):
    c, _, _ = client(tmp_path)
    r = c.get("/slots/calendar", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/slots/login"
    c.get("/slots/dev-login?email=eve@students.example.edu")
    assert c.get("/slots/", follow_redirects=False).status_code == 303


def test_pages_render_for_admin(tmp_path):
    c, db, _ = client(tmp_path)
    db.set_setting("ssh_enforce", "1")
    add_server(db, "hs1")
    store_report(db, "hs1", {"errors": [],
                             "nodes": [{"name": "pc-41", "tags": ["tag:pc-41"], "online": True, "ipv4": "100.64.0.2"}],
                             "pc_state": {"1": {"active_account": "grp-thesis-a", "last_error": None}}})
    c.get("/slots/dev-login?email=boss@example.edu")
    for path in ("/slots/", "/slots/calendar", "/slots/calendar?week=2026-10-26",
                 "/slots/enrollments", "/slots/pcs",
                 "/slots/terms", "/slots/settings", "/slots/audit", "/slots/pcs/setup-lab-pc.sh"):
        r = c.get(path)
        assert r.status_code == 200, path
    cal = c.get("/slots/calendar?week=2026-10-19").text
    assert "TH-A" in cal and "Edit in the Sheet" in cal
    nav = c.get("/slots/").text
    assert all(f'>{x}<' in nav or f'>{x}<span' in nav for x in ("Now", "Calendar", "Enrollments", "PCs", "Semesters",
                                                               "Settings", "Log"))
    assert ">Schedule<" not in nav and ">Groups<" not in nav
    for old in ("/slots/grid", "/slots/groups", "/slots/groups/1"):          # old links land on the Calendar
        assert c.get(old, follow_redirects=False).headers["location"] == "/slots/calendar"
    now = c.get("/slots/").text
    assert "grp-thesis-a" in now and "100.64.0.2" in now
    script = c.get("/slots/pcs/setup-lab-pc.sh").text   # served as is: Tailscale SSH, no key to fill in
    assert "tailscale set --ssh" in script and "PUBKEY" not in script
    assert "hs1: policy its agent would write now" in c.get("/slots/settings").text


def test_editing_moved_to_the_sheet(tmp_path):
    c, db, _ = client(tmp_path)
    c.get("/slots/dev-login?email=boss@example.edu")
    for path in ("/slots/terms", "/slots/grid/cell", "/slots/pcs", "/slots/settings/admins", "/slots/groups/1/delete"):
        assert c.post(path, data={"name": "X"}, headers=ORIGIN).status_code in (404, 405), path
    assert c.post("/slots/groups/1", data={"name": "x"}, headers=ORIGIN).status_code == 405


def test_cross_site_post_refused(tmp_path):
    c, _, _ = client(tmp_path)
    c.get("/slots/dev-login?email=boss@example.edu")
    r = c.post("/slots/settings", data={"enforce_policy": "1"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_settings_rejects_bad_extra_grants(tmp_path):
    c, db, _ = client(tmp_path)
    c.get("/slots/dev-login?email=boss@example.edu")
    c.post("/slots/settings", data={"enforce_policy": "1", "extra": '[{"src": ["x"]}]', "ports": "tcp:22"},
           headers=ORIGIN)
    assert db.setting("extra_grants") == "[]"
    c.post("/slots/settings", data={"enforce_policy": "1", "ports": "tcp:22",
                                    "extra": '[{"src":["autogroup:member"],"dst":["autogroup:self"],"ip":["*"]}]'},
           headers=ORIGIN)
    assert "autogroup:self" in db.setting("extra_grants")
    c.post("/slots/settings", data={"enforce_policy": "1", "ports": "tcp:22", "extra": "[]",
                                    "extra_ssh": '[{"src": ["tag:jumphost"], "dst": ["*"], "users": ["root"]}]'},
           headers=ORIGIN)
    assert db.setting("extra_ssh") == "[]" and "autogroup:self" in db.setting("extra_grants")  # nothing saved
    jump = '[{"src": ["tag:jumphost"], "dst": ["tag:pc-41"], "users": ["autogroup:nonroot"]}]'
    c.post("/slots/settings", data={"enforce_policy": "1", "ports": "tcp:22", "extra": "[]", "extra_ssh": jump},
           headers=ORIGIN)
    assert json.loads(db.setting("extra_ssh")) == json.loads(jump)
    assert "tag:jumphost" in c.get("/slots/settings").text


def test_servers_tokens_and_agent_api(tmp_path):
    c, db, _ = client(tmp_path)
    c.get("/slots/dev-login?email=boss@example.edu")
    c.post("/slots/settings/servers", data={"name": "hs1", "action": "add"}, headers=ORIGIN, follow_redirects=False)
    page = c.get("/slots/settings").text
    token = page.split('<pre style="user-select: all">')[1].split("</pre>")[0]
    assert len(token) > 30 and token not in c.get("/slots/settings").text      # shown once
    assert c.get("/slots/api/agent/state").status_code == 401
    assert c.get("/slots/api/agent/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
    auth = {"Authorization": f"Bearer {token}"}
    st = c.get("/slots/api/agent/state", headers=auth).json()
    assert st["server"] == "hs1" and {p["name"] for p in st["pcs"]} == {"pc-41", "pc-43"}
    assert {g["slug"] for g in st["groups"]} == {"thesis-a", "thesis-b"} and len(st["bookings"]) == 2
    assert c.post("/slots/api/agent/report", content="nope", headers=auth).status_code == 400
    assert c.post("/slots/api/agent/report", content=json.dumps({"errors": ["boom"]}), headers=auth).json()["ok"]
    assert "boom" in c.get("/slots/").text
    c.post("/slots/settings/servers", data={"name": "hs1", "action": "remove"}, headers=ORIGIN)
    assert db.server_names() == ["hs1"]                                          # still has PCs


def test_enforce_now_asks_agents(tmp_path):
    c, db, _ = client(tmp_path)
    c.get("/slots/dev-login?email=boss@example.edu")
    c.post("/slots/enforce", headers=ORIGIN)
    assert db.setting("force_seq") == "1"


ENROLL = {"group_name": "Bangla ASR", "thesis_title": "Low-resource Bangla speech recognition",
         "s_name": ["Dr. Alam"], "s_email": ["alam@example.edu"], "hardware": "1 GPU, 12 GB", "notes": "",
         "m_name": ["Rahim Uddin", "Karim Ali"], "m_id": ["21101001", "21101002"],
         "m_email": ["rahim@students.example.edu", "karim"]}   # the form takes the part before @students.example.edu


def test_student_enrols_and_cannot_see_admin_pages(tmp_path):
    c, db, _ = client(tmp_path)
    assert "Sign in with Google" in c.get("/slots/enroll").text or "Dev sign-in" in c.get("/slots/enroll").text
    c.get("/slots/dev-login?email=rahim@students.example.edu&next=/enroll")
    assert c.get("/slots/grid", follow_redirects=False).status_code == 303   # admin pages stay closed
    assert "Enrollment form" in c.get("/slots/enroll").text
    r = c.post("/slots/enroll", data=ENROLL, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    e = db.enrollment(1)
    assert e["status"] == "pending" and [m["email"] for m in e["members"]] == ["rahim@students.example.edu", "karim@students.example.edu"]
    page = c.get("/slots/enroll").text
    assert "Waiting for an admin" in page and "Enrollment form" not in page   # one active enrollment at a time


def test_enrol_validation(tmp_path):
    c, db, _ = client(tmp_path)
    c.get("/slots/dev-login?email=rahim@students.example.edu&next=/enroll")
    bad = dict(ENROLL, m_id=["123", "21101002"], m_email=["someone@gmail.com", "karim@students.example.edu"])
    c.post("/slots/enroll", data=bad, headers=ORIGIN, follow_redirects=False)
    assert db.one("SELECT COUNT(*) AS n FROM enrolments")["n"] == 0
    page = c.get("/slots/enroll").text
    assert "student ID is 8 digits" in page and "Include yourself" in page and "not a personal one" in page
    assert 'value="21101002"' in page                                     # what was typed comes back


def test_enrol_needs_g_suite_members_and_a_supervisor(tmp_path):
    c, db, _ = client(tmp_path)
    c.get("/slots/dev-login?email=rahim@students.example.edu&next=/enroll")
    form = c.get("/slots/enroll").text
    assert form.count('name="m_name"') == 5 and "@students.example.edu" in form and "Add co-supervisor" in form
    for bad in (dict(ENROLL, m_email=["rahim@students.example.edu", "karim@example.edu"]),    # staff domain, not a student
                dict(ENROLL, m_email=["rahim@students.example.edu", "karim@gmail.com"]),      # personal address
                dict(ENROLL, s_name=[""], s_email=[""]),                               # no supervisor
                dict(ENROLL, s_name=["Dr. Alam"], s_email=["not-an-email"])):
        c.post("/slots/enroll", data=bad, headers=ORIGIN)
        assert db.one("SELECT COUNT(*) AS n FROM enrolments")["n"] == 0, bad
    six = dict(ENROLL, m_name=[f"S{i}" for i in range(6)], m_id=[f"2110100{i}" for i in range(6)],
               m_email=["rahim@students.example.edu"] + [f"s{i}" for i in range(1, 6)])
    c.post("/slots/enroll", data=dict(six, s_name=["Dr. Alam", "Dr. Saif", ""], s_email=["alam@example.edu", "saif@example.edu", ""]),
           headers=ORIGIN)
    e = db.enrollment(1)
    assert len(e["members"]) == 5                                         # a group is at most five
    assert e["members"][1]["email"] == "s1@students.example.edu"
    assert e["supervisors"] == [{"name": "Dr. Alam", "email": "alam@example.edu"},
                                {"name": "Dr. Saif", "email": "saif@example.edu"}]
    assert (e["supervisor"], e["co_supervisor"]) == ("Dr. Alam", "Dr. Saif")
    assert "supervisors Dr. Alam, Dr. Saif" in c.get("/slots/enroll").text


def test_personal_google_account_cannot_sign_in_to_enroll(tmp_path):
    c, _, _ = client(tmp_path)
    c.get("/slots/dev-login?email=someone@gmail.com&next=/enroll")
    assert "Enrollment form" not in c.get("/slots/enroll").text
    c.get("/slots/dev-login?email=lecturer@example.edu&next=/enroll")       # staff, not a student
    assert "Enrollment form" not in c.get("/slots/enroll").text


def test_email_settings_and_test_email(tmp_path, monkeypatch):
    from labslots.db import DB
    from labslots.mailer import Mailer
    c, db, _ = client(tmp_path)
    c.get("/slots/dev-login?email=boss@example.edu")
    page = c.get("/slots/settings").text
    assert "Email is off" in page and "smtp_password" in page
    c.post("/slots/settings/email-test", headers=ORIGIN)
    assert db.one("SELECT 1 FROM audit WHERE action = 'email-test'") is None   # nothing to send with

    sent = []
    monkeypatch.setattr(Mailer, "send", lambda self, to, subject, body, done=None: (sent.append(to), done(True, "ok")))
    on = cfg(tmp_path)
    on.smtp_host, on.smtp_from = "smtp.gmail.com", "lab@students.example.edu"
    c2 = TestClient(create_app(on, db=DB(tmp_path / "mail.db"), clock=Clock(MON_10), sheet=FakeSheets()))
    c2.get("/slots/dev-login?email=boss@example.edu")
    assert "Sent from <code>lab@students.example.edu</code>" in c2.get("/slots/settings").text
    c2.post("/slots/settings/email-test", headers=ORIGIN)
    assert sent == [["boss@example.edu"]]
    assert c2.app.state.db.one("SELECT 1 FROM audit WHERE action = 'email-test-sent'")


def test_admin_approves_enrollment_into_the_sheet(tmp_path):
    c, db, sheet = client(tmp_path)
    s = TestClient(c.app)
    s.get("/slots/dev-login?email=rahim@students.example.edu&next=/enroll")
    s.post("/slots/enroll", data=ENROLL, headers=ORIGIN)
    c.get("/slots/dev-login?email=boss@example.edu")
    assert "Bangla ASR" in c.get("/slots/enrollments").text and c.get("/slots/enrollments/1").status_code == 200
    assert "TH-A" in c.get("/slots/enrollments/1/slots?term=1&pc=1").text
    # Monday day on pc-41 is taken by Thesis A: refused
    c.post("/slots/enrollments/1/approve", data={"slug": "bangla-asr", "short": "", "color": "#b8e6b0", "term": 1,
                                                "pc": 1, "slot": ["0-day", "2-day"]}, headers=ORIGIN)
    assert db.enrollment(1)["status"] == "pending"
    c.post("/slots/enrollments/1/approve", data={"slug": "bangla-asr", "short": "", "color": "#b8e6b0", "term": 1,
                                                "pc": 2, "slot": ["2-day", "4-night"]}, headers=ORIGIN)
    e = db.enrollment(1)
    assert e["status"] == "approved" and e["group_id"]
    g = db.one("SELECT * FROM groups WHERE id = ?", (e["group_id"],))
    assert g["linux_account"] == "grp-bangla-asr" and g["short"] == "BA-ASR"
    assert any(r and r[0] == "bangla-asr" for r in sheet.tabs[GROUPS])
    row = next(r for r in sheet.tabs[routine_tab("T")] if r and r[0] == "pc-43")
    assert row[5] == "BA-ASR" and row[10] == "BA-ASR"
    status = s.get("/slots/enroll").text
    assert "Enrolled" in status and "pc-43" in status and "grp-bangla-asr" in status and "Wed 06:00-17:55" in status


def test_enrollment_shows_the_group_name_from_the_sheet(tmp_path):
    c, db, sheet = client(tmp_path)
    s = TestClient(c.app)
    s.get("/slots/dev-login?email=rahim@students.example.edu&next=/enroll")
    s.post("/slots/enroll", data=ENROLL, headers=ORIGIN)
    c.get("/slots/dev-login?email=boss@example.edu")
    c.post("/slots/enrollments/1/approve", data={"slug": "bangla-asr", "term": 1, "pc": 2, "slot": ["2-day"]},
           headers=ORIGIN)
    next(r for r in sheet.tabs[GROUPS] if r and r[0] == "bangla-asr")[1] = "Speech Lab"
    c.app.state.sync.pull()
    assert "Speech Lab" in c.get("/slots/enrollments").text
    detail = c.get("/slots/enrollments/1").text
    assert "Speech Lab" in detail and "Sent as Bangla ASR" in detail


def test_no_student_keys_and_reject_flow(tmp_path):
    c, db, _ = client(tmp_path)
    ids = {"p2": 2}
    s = TestClient(c.app)
    s.get("/slots/dev-login?email=rahim@students.example.edu&next=/enroll")
    s.post("/slots/enroll", data=ENROLL, headers=ORIGIN)
    c.get("/slots/dev-login?email=boss@example.edu")
    c.post("/slots/enrollments/1/approve", data={"slug": "bangla-asr", "term": 1, "pc": ids["p2"], "slot": ["0-day"]},
           headers=ORIGIN)
    # Tailscale SSH checks the tailnet identity: there is nowhere to upload keys
    assert s.post("/slots/enroll/1/keys", data={"ssh_keys": "ssh-ed25519 AAAAC3Nza rahim@laptop"},
                  headers=ORIGIN).status_code in (404, 405)
    assert "No SSH key or password" in s.get("/slots/enroll").text
    # a second group gets rejected with a reason the student can read
    t = TestClient(c.app)
    t.get("/slots/dev-login?email=nadia@students.example.edu&next=/enroll")
    t.post("/slots/enroll", data=dict(ENROLL, group_name="Vision", m_name=["Nadia", "Tanvir"], m_id=["21101009", "21101010"],
                                      m_email=["nadia@students.example.edu", "tanvir"]), headers=ORIGIN)
    c.post("/slots/enrollments/2/reject", data={"note": "Ask your supervisor to confirm"}, headers=ORIGIN)
    assert db.enrollment(2)["status"] == "rejected"
    page = t.get("/slots/enroll").text
    assert "Not approved" in page and "Ask your supervisor to confirm" in page and "Send a new enrollment" in page


def test_google_client_signs_in_and_reads(tmp_path):
    import json as _json

    import httpx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from labslots.sheets import Sheets
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    key = tmp_path / "sa.json"
    key.write_text(_json.dumps({"client_email": "sync@p.iam.gserviceaccount.com", "private_key": pem,
                                "private_key_id": "k1", "token_uri": "https://oauth2.googleapis.com/token"}))
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.host == "oauth2.googleapis.com":
            assert b"jwt-bearer" in req.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.path.endswith("values:batchGet"):
            return httpx.Response(200, json={"valueRanges": [{"values": [["Email"], ["a@b.c"]]}]})
        return httpx.Response(200, json={"sheets": [{"properties": {"title": "Admins", "sheetId": 7}}]})

    sh = Sheets(str(key), "https://docs.google.com/spreadsheets/d/SHEET123/edit")
    sh.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert sh.tab_ids() == {"Admins": 7}
    assert sh.read(["Admins"]) == {"Admins": [["Email"], ["a@b.c"]]}
    assert sum(1 for r in seen if r.url.host == "oauth2.googleapis.com") == 1   # token reused
    assert "SHEET123" in str(seen[-1].url)


def test_calendar_week_month_and_three_month_views(tmp_path):
    c, db, _ = client(tmp_path)
    c.get("/slots/dev-login?email=boss@example.edu")
    week = c.get("/slots/calendar?view=week&at=2026-10-21").text
    assert "Week of 19 October 2026" in week and "TH-A" in week
    month = c.get("/slots/calendar?view=month&at=2026-10-21").text
    assert "<h1>October 2026</h1>" in month and month.count('class="mcal-day') == 5 * 7   # 27 Sep to 31 Oct
    assert 'aria-current="true">Month<' in month
    assert '<b>1</b><span class="occ-of">/2</span>' in month and "<b>TH-A</b>" not in month  # unfiltered: how full
    by_a = c.get("/slots/calendar?view=month&at=2026-10-21&group=1").text
    assert by_a.count("<b>TH-A</b>") == 4 and "pc-41 · day" in by_a     # filtered: the group's slots, Mondays
    day = c.get("/slots/calendar?view=day&at=2026-10-19").text
    assert "Monday 19 October 2026" in day and "TH-A" in day and "TH-B" in day and "data-search" in day
    assert "2 of 2 PCs" not in day and "<b>1</b> of 2 PCs booked by day" in day
    quarter = c.get("/slots/calendar?view=quarter&at=2026-12-05").text
    assert "<h1>December to February 2027</h1>" in quarter
    assert all(f'aria-label="{m}' in quarter for m in ("December 2026", "January 2027", "February 2027"))
    assert "noterm" in quarter                                                          # term T ends 31 Dec
    assert 'at=2026-09-01' in quarter and 'at=2027-03-01' in quarter                    # previous / next 3 months
    only_b = c.get("/slots/calendar?view=month&at=2026-10-01&group=2").text
    assert "<b>TH-B</b>" in only_b and "<b>TH-A</b>" not in only_b
    only_41 = c.get("/slots/calendar?view=month&at=2026-10-01&pc=1").text
    assert "pc-41 · day" in only_41 and "pc-43 · " not in only_41
    assert c.get("/slots/calendar?view=nonsense&at=bad").status_code == 200
    assert "style.css?v=" in month


def test_oauth_client_json_as_downloaded(tmp_path):
    from labslots.config import oauth_client
    f = tmp_path / "client.json"
    f.write_text(json.dumps({"web": {"client_id": "123.apps.googleusercontent.com", "client_secret": "GOCSPX-x",
                                     "redirect_uris": ["https://hs.example.edu/slots/auth/callback"]}}))
    assert oauth_client(str(f)) == ("123.apps.googleusercontent.com", "GOCSPX-x")
    assert oauth_client(str(tmp_path / "missing.json")) == ("", "")


def test_rotated_service_account_key_is_picked_up(tmp_path):
    import httpx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from labslots.sheets import Sheets

    def key(email):
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        return json.dumps({"client_email": email, "private_key": pem, "private_key_id": email[:2]})
    f = tmp_path / "google-service-account.json"
    f.write_text(key("old@p.iam.gserviceaccount.com"))
    signins = []

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            signins.append(req)
            return httpx.Response(200, json={"access_token": f"tok{len(signins)}", "expires_in": 3600})
        return httpx.Response(200, json={"sheets": []})
    sh = Sheets(str(f), "SHEET")
    sh.http = httpx.Client(transport=httpx.MockTransport(handler))
    sh.tab_ids()
    sh.tab_ids()
    assert len(signins) == 1 and sh.email.startswith("old@")
    tmp = tmp_path / "new.json"
    tmp.write_text(key("new@p.iam.gserviceaccount.com"))
    tmp.replace(f)                                   # a new download dropped in under the same name
    sh.tab_ids()
    assert len(signins) == 2 and sh.email.startswith("new@")


def test_groups_of_one_to_five(tmp_path):
    c, db, _ = client(tmp_path)
    c.get("/slots/dev-login?email=rahim@students.example.edu&next=/enroll")
    form = c.get("/slots/enroll").text
    assert "Up to 5 members" in form and form.count("placeholder=Optional") == 4   # rows 2 to 5
    three = dict(ENROLL, m_name=["Rahim Uddin", "Karim Ali", "Nadia Islam", "", ""],
                 m_id=["21101001", "21101002", "21101003", "", ""], m_email=["rahim@students.example.edu", "karim", "nadia", "", ""])
    c.post("/slots/enroll", data=three, headers=ORIGIN)
    assert len(db.enrollment(1)["members"]) == 3                       # empty rows are simply left out
    (tmp_path / "solo").mkdir()
    c2, db2, _ = client(tmp_path / "solo")
    c2.get("/slots/dev-login?email=rahim@students.example.edu&next=/enroll")
    c2.post("/slots/enroll", data=dict(ENROLL, m_name=["Rahim Uddin"], m_id=["21101001"], m_email=["rahim@students.example.edu"]),
            headers=ORIGIN)
    assert [m["email"] for m in db2.enrollment(1)["members"]] == ["rahim@students.example.edu"]   # working alone


def test_old_enrolments_links_still_work(tmp_path):
    c, _, _ = client(tmp_path)
    c.get("/slots/dev-login?email=boss@example.edu")
    assert c.get("/slots/enrolments", follow_redirects=False).headers["location"] == "/slots/enrollments"
    assert c.get("/slots/enrolments/3", follow_redirects=False).headers["location"] == "/slots/enrollments/3"
    page = c.get("/slots/enrollments").text
    assert "Enrollments" in page and "Enrolment" not in page
