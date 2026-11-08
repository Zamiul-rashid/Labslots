"""Admin web app: weekly grid, calendar view, approvals, groups, PCs, settings."""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from datetime import date, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import Config
from .db import DB, now_iso, short_code
from .enforcer import Enforcer
from .hub import SERVER_RE, add_server, apply_slice, node_index, pc_states, reports, server_for_token, \
    server_slice, store_report
from .mailer import Mailer
from .policy import SLUG_RE, TAG_RE, PolicyError, build_policy
from .slots import HALVES, WEEKDAYS, Slot, in_switch_window, monday_of, next_slot, slot_at, span, week_slots
from .sync import AssignError, Sync, next_color

HERE = Path(__file__).parent
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACCOUNT_RE = re.compile(r"^grp-[a-z0-9-]{1,28}$")
log = logging.getLogger("labslots.web")


class NeedLogin(Exception):
    pass


def create_app(cfg: Config, db: DB | None = None, clock=datetime.now, sheet=None) -> FastAPI:
    """The hub: enrollments, status and the Sheet sync. Enforcement runs in each server's slot agent."""
    db = db or DB(cfg.db_path)
    for email in cfg.bootstrap_admins:
        db.x("INSERT OR IGNORE INTO admins(email, added_at, added_by) VALUES (?, ?, 'bootstrap')",
             (email, now_iso()))

    secret = cfg.session_secret
    if not secret:
        secret_file = Path(cfg.db_path).with_name("session_secret")
        if secret_file.exists():
            secret = secret_file.read_text().strip()
        else:
            secret = secrets.token_urlsafe(48)
            secret_file.write_text(secret)
            secret_file.chmod(0o600)

    enforcer = Enforcer(db, None, None, clock)  # planning only (active bookings, next change); agents enforce
    if sheet is None and cfg.sheet_id and cfg.google_key_file and Path(cfg.google_key_file).exists():
        from .sheets import Sheets
        sheet = Sheets(cfg.google_key_file, cfg.sheet_id)
    sync = Sync(db, sheet, cfg.bootstrap_admins) if sheet is not None else None
    mailer = Mailer(cfg.smtp_host, cfg.smtp_port, cfg.smtp_user, cfg.smtp_password, cfg.smtp_from)
    base = cfg.base_path
    app = FastAPI(title="labslots", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="labslots",
                       path=base or "/", same_site="lax",
                       https_only=cfg.public_url.startswith("https"), max_age=12 * 3600)
    app.mount(f"{base}/static", StaticFiles(directory=HERE / "static"), name="static")
    # a fingerprint in the stylesheet's URL, so browsers fetch the new one after every deploy
    import hashlib as _h
    static_v = _h.sha256((HERE / "static" / "style.css").read_bytes()).hexdigest()[:10]
    templates = Jinja2Templates(directory=HERE / "templates")
    r = APIRouter(prefix=base)
    app.state.db, app.state.enforcer, app.state.sync = db, enforcer, sync

    oauth = None
    if cfg.oidc_client_id and cfg.oidc_client_secret:
        from authlib.integrations.starlette_client import OAuth
        oauth = OAuth()
        oauth.register("google", client_id=cfg.oidc_client_id, client_secret=cfg.oidc_client_secret,
                       server_metadata_url=f"{cfg.oidc_issuer}/.well-known/openid-configuration",
                       client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"})

    # -- plumbing ----------------------------------------------------------------
    @app.exception_handler(NeedLogin)
    async def _need_login(request: Request, _exc: NeedLogin):
        return RedirectResponse(f"{base}/login", status_code=303)

    def admin(request: Request) -> str:
        email = request.session.get("email", "")
        if not email or not db.is_admin(email):
            raise NeedLogin()
        return email

    def same_origin(request: Request) -> None:
        allowed = {urlparse(cfg.public_url).netloc, request.headers.get("host", "")}
        src = request.headers.get("origin") or request.headers.get("referer") or ""
        if urlparse(src).netloc not in allowed:
            raise HTTPException(403, "cross-site request refused")

    def flash(request: Request, msg: str, kind: str = "ok") -> None:
        request.session.setdefault("flash", []).append([kind, msg])

    def status_line() -> dict:
        pending = db.one("SELECT COUNT(*) AS n FROM enrolments WHERE status = 'pending'")["n"]
        reps = reports(db, clock())
        return {"next_change": enforcer.next_change(), "pending": pending,
                "flags": {k: db.flag(k) for k in ("enforce_policy", "device_cleanup", "ssh_enforce")},
                "errors": sum(len(r["report"].get("errors", [])) for r in reps if not r["stale"]),
                "admins_only": any(r["report"].get("admins_only") for r in reps if not r["stale"]),
                "quiet": [r["name"] for r in reps if r["stale"]], "servers": len(reps)}

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        msgs = request.session.pop("flash", [])
        user = request.session.get("email", "")
        signed_in = bool(user) and db.is_admin(user)
        return templates.TemplateResponse(request, name, {
            "base": base, "lab_name": cfg.lab_name, "org": cfg.org_name, "user": user if signed_in else "", "viewer": user, "flashes": msgs,
            "status": status_line() if signed_in and not name.startswith("_") else None,
            "sheet": sync.status() if sync and signed_in else None,
            "now_slot": slot_at(clock()), "switching": in_switch_window(clock()), "WEEKDAYS": WEEKDAYS, "HALVES": HALVES,
            "SPAN": {h: span(h) for h in HALVES}, "static_v": static_v, **ctx})

    def is_student_email(email: str) -> bool:
        return email.rsplit("@", 1)[-1] in cfg.student_domains

    def viewer(request: Request) -> str:
        """Any signed-in student account or admin (enrollment pages)."""
        email = request.session.get("email", "")
        if not email or not (db.is_admin(email) or is_student_email(email)):
            raise NeedLogin()
        return email

    def back(path: str) -> RedirectResponse:
        return RedirectResponse(f"{base}{path}", status_code=303)

    def pick_term(term_id: int | None):
        terms = db.terms()
        if term_id:
            t = db.one("SELECT * FROM terms WHERE id = ?", (term_id,))
            if t:
                return t, terms
        on = db.terms_on(clock().date())
        return (on[0] if on else (terms[0] if terms else None)), terms

    # -- auth ----------------------------------------------------------------------
    @r.get("/login", response_class=HTMLResponse)
    async def login(request: Request):
        return page(request, "login.html", oidc=oauth is not None, dev=cfg.dev_login)

    @r.get("/login/google")
    async def login_google(request: Request):
        if oauth is None:
            raise HTTPException(503, "Google sign-in is not configured")
        # select_account: always show Google's account picker (personal vs university account).
        return await oauth.google.authorize_redirect(request, f"{cfg.public_url}{base}/auth/callback",
                                                     prompt="select_account")

    @r.get("/auth/callback")
    async def auth_callback(request: Request):
        if oauth is None:
            raise HTTPException(503)
        try:
            token = await oauth.google.authorize_access_token(request)
        except Exception as e:  # noqa: BLE001
            flash(request, f"Sign-in failed: {e}", "err")
            return back("/login")
        info = token.get("userinfo") or {}
        email = (info.get("email") or "").lower()
        if not email or not info.get("email_verified"):
            flash(request, "Google did not return a verified email.", "err")
            return back("/login")
        if db.is_admin(email):
            request.session["email"] = email
            db.audit(email, "login", "")
            return back("/")
        if is_student_email(email):
            request.session["email"] = email
            db.audit(email, "login-student", "not on the admin list: sent to the enrollment page")
            return back("/enroll")
        db.audit(email, "login-refused", "not an admin or student account")
        flash(request, f"{email} cannot sign in here. Use your {cfg.org_name} Google account.", "err")
        return back("/login")

    @r.get("/dev-login")
    async def dev_login(request: Request, email: str, next: str = "/"):
        if not cfg.dev_login:
            raise HTTPException(404)
        request.session["email"] = email.lower()
        return back(next if next.startswith("/") and not next.startswith("//") else "/")

    @r.post("/logout")
    async def logout(request: Request):
        same_origin(request)
        was_admin = db.is_admin(request.session.get("email", ""))
        request.session.clear()
        return back("/login" if was_admin else "/enroll")

    # -- dashboard -----------------------------------------------------------------
    @r.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        admin(request)
        now = clock()
        slot = slot_at(now)
        active = {b["pc_id"]: b for b in enforcer.active_bookings(slot)}
        upcoming = next_slot(slot)
        nxt = {b["pc_id"]: b for b in enforcer.active_bookings(upcoming)}
        reps = reports(db, now)
        by_tag, states = node_index(reps), pc_states(reps)
        known = {r["name"] for r in reps}
        rows = [{"pc": p, "booking": active.get(p["id"]), "next": nxt.get(p["id"]), "server": db.server_of(p),
                 "unserved": db.server_of(p) not in known,
                 "node": by_tag.get(p["tag"]), "state": states.get(p["id"])}
                for p in sorted(db.pcs(), key=lambda p_: natural(p_["name"]))]
        for r_ in rows:
            r_["problem"] = bool(r_["unserved"] or (r_["state"] and r_["state"].get("last_error")))
        rows.sort(key=lambda r_: not r_["problem"])  # problems first, then by PC number
        counts = {"total": len(rows), "busy": sum(1 for r_ in rows if r_["booking"]),
                  "online": sum(1 for r_ in rows if r_["node"] and r_["node"].get("online")),
                  "problems": sum(1 for r_ in rows if r_["problem"])}
        return page(request, "dashboard.html", slot=slot, upcoming=upcoming, rows=rows, servers=reps, counts=counts,
                    next_change=enforcer.next_change(), rotation=enforcer.rotation())

    @r.post("/enforce")
    async def enforce_now(request: Request):
        who = admin(request)
        same_origin(request)
        db.set_setting("force_seq", str(int(db.setting("force_seq") or 0) + 1))
        db.audit(who, "enforce-now", "asked every server's agent to re-apply")
        flash(request, "Asked every server's slot agent to re-apply the policy and PC accounts. "
                       "It happens on their next check, within about 15 seconds.")
        return back("/")

    # -- pages folded into the Calendar and the Sheet -------------------------------------
    @r.get("/grid")
    @r.get("/groups")
    @r.get("/groups/{gid}")
    async def folded(request: Request):
        admin(request)
        return back("/calendar")

    @r.get("/enrolments")
    @r.get("/enrolments/{eid}")
    async def old_spelling(request: Request, eid: int | None = None):
        """Links from before the spelling became "enrollment" (emails, bookmarks)."""
        admin(request)
        return back(f"/enrollments/{eid}" if eid else "/enrollments")

    # -- calendar view -------------------------------------------------------------------
    @r.get("/calendar", response_class=HTMLResponse)
    async def calendar(request: Request, view: str = "week", at: str = "", week: str = "", group: int = 0,
                       pc: int = 0):
        admin(request)
        view = view if view in ("day", "week", "month", "quarter") else "week"
        try:
            d = date.fromisoformat(at or week) if (at or week) else clock().date()
        except ValueError:
            d = clock().date()
        if view == "day":
            start = first = last = d
            prev, nxt = d - timedelta(days=1), d + timedelta(days=1)
            title = d.strftime("%A %d %B %Y")
            months = []
        elif view == "week":
            start = monday_of(d)
            first, last = start, start + timedelta(days=6)
            prev, nxt = start - timedelta(days=7), start + timedelta(days=7)
            title = f"Week of {start.strftime('%d %B %Y')}"
            months = []
        else:
            span = 1 if view == "month" else 3
            start = d.replace(day=1)
            prev, nxt = add_months(start, -span), add_months(start, span)
            months = [month_grid(add_months(start, k)) for k in range(span)]
            first, last = months[0]["weeks"][0][0], months[-1]["weeks"][-1][-1]
            title = start.strftime("%B %Y") if span == 1 else \
                f"{start.strftime('%B')} to {add_months(start, 2).strftime('%B %Y')}"
        terms = db.terms()
        pcs_all = sorted(db.pcs(), key=lambda p_: natural(p_["name"]))
        pc_name = {p_["id"]: p_["name"] for p_ in pcs_all}
        booked = {t["id"]: [b for b in db.bookings(t["id"], ("approved",))
                            if (not group or b["group_id"] == group) and (not pc or b["pc_id"] == pc)]
                  for t in terms}
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        cal: dict[tuple, list] = {}
        entries: dict[date, list] = {}
        in_term = set()
        for day in days:
            iso = day.isoformat()
            for t in terms:
                if t["start_date"] <= iso <= t["end_date"]:
                    in_term.add(day)
                    for b in booked[t["id"]]:
                        if b["weekday"] == day.weekday():
                            cal.setdefault((b["pc_id"], day, b["half"]), []).append(b)
                            entries.setdefault(day, []).append({"pc": pc_name.get(b["pc_id"], "?"), "half": b["half"],
                                                                "b": b})
        for v in entries.values():
            v.sort(key=lambda e: (e["half"] != "day", natural(e["pc"])))
        shown = {b["group_id"] for v in cal.values() for b in v}
        n_pcs = 1 if pc else len(pcs_all)
        occ = {day: {h: sum(1 for e in entries.get(day, []) if e["half"] == h) for h in HALVES} for day in days}
        return page(request, "calendar.html", view=view, title=title, start=start, n_pcs=n_pcs, occ=occ,
                    detail=bool(group or pc), days=days if view in ("day", "week") else [], months=months,
                    entries=entries,
                    pcs=[p_ for p_ in pcs_all if not pc or p_["id"] == pc], all_pcs=pcs_all, cal=cal,
                    in_term=in_term, groups=db.groups(), group=group, pc=pc, prev=prev.isoformat(),
                    next=nxt.isoformat(), today=clock().date(), current=slot_at(clock()),
                    legend=[g for g in db.groups() if g["id"] in shown])

    # -- PCs -------------------------------------------------------------------------------
    @r.get("/pcs", response_class=HTMLResponse)
    async def pcs_page(request: Request):
        admin(request)
        reps = reports(db, clock())
        by_tag, states = node_index(reps), pc_states(reps)
        known = {p["tag"] for p in db.pcs()}
        unregistered = sorted(set(by_tag) - known - {"tag:scheduler", "tag:jumphost"})
        return page(request, "pcs.html", pcs=db.pcs(), by_tag=by_tag, unregistered=unregistered,
                    states=states, servers=reps, server_of=db.server_of)

    @r.get("/pcs/setup-lab-pc.sh", response_class=PlainTextResponse)
    async def setup_script(request: Request):
        """The PC setup script. It holds no key, so one script serves every server."""
        admin(request)
        return PlainTextResponse((HERE / "pc" / "setup-lab-pc.sh").read_text(),
                                 headers={"Content-Disposition": "attachment; filename=setup-lab-pc.sh"})

    # -- terms ------------------------------------------------------------------------------
    @r.get("/terms", response_class=HTMLResponse)
    async def terms_page(request: Request):
        admin(request)
        return page(request, "terms.html", terms=db.terms())

    # -- settings ------------------------------------------------------------------------------
    @r.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        admin(request)
        reps = reports(db, clock())
        policies = []
        for r_ in reps:
            policies.append({"name": r_["name"], "preview": preview_for(r_["name"]),
                             "live": r_["report"].get("live_policy", ""), "applied_at": r_["report"].get("applied_at", "")})
        return page(request, "settings.html", admins=db.admins(), s={k: db.setting(k) for k in (
            "enforce_policy", "device_cleanup", "former_cleanup", "ssh_enforce", "max_devices_per_student")},
            ports=", ".join(json.loads(db.setting("slot_ports") or "[]")),
            extra=json.dumps(json.loads(db.setting("extra_grants") or "[]"), indent=2),
            extra_ssh=json.dumps(json.loads(db.setting("extra_ssh") or "[]"), indent=2),
            policies=policies, servers=reps, default_server=db.setting("default_server"),
            new_token=request.session.pop("new_token", None),
            rotation=enforcer.rotation(), pcs=db.pcs(), groups=db.groups(),
            mail={"on": mailer.enabled, "host": cfg.smtp_host, "port": cfg.smtp_port, "user": cfg.smtp_user,
                  "sender": cfg.smtp_from})

    def preview_for(server: str) -> str:
        """The policy that server's agent would write now, built from its slice like the agent does."""
        try:
            mem = DB(":memory:")
            apply_slice(mem, server_slice(db, server))
            return Enforcer(mem, None, None, clock).preview()
        except PolicyError as e:
            return f"// policy cannot be built: {e}"
        except Exception as e:  # noqa: BLE001
            return f"// preview failed: {e}"

    @r.post("/settings/servers")
    async def servers_save(request: Request, name: str = Form(""), action: str = Form("add")):
        who = admin(request)
        same_origin(request)
        name = name.strip().lower()
        if not SERVER_RE.match(name):
            flash(request, "Server names are lowercase letters, digits and dashes, such as hs2.", "err")
            return back("/settings#servers")
        if action == "remove":
            if any(db.server_of(p) == name for p in db.pcs()):
                flash(request, f"PCs are still on {name}. Move them to another server in the Sheet first.", "err")
                return back("/settings#servers")
            db.x("DELETE FROM servers WHERE name = ?", (name,))
            db.audit(who, "server-removed", name)
            flash(request, f"Removed {name}. Its agent can no longer fetch the schedule.")
            return back("/settings#servers")
        existed = name in db.server_names()
        token = add_server(db, name)
        db.audit(who, "server-rekeyed" if existed else "server-added", name)
        request.session["new_token"] = {"name": name, "token": token}
        return back("/settings#servers")

    @r.post("/settings")
    async def settings_save(request: Request, enforce_policy: str = Form(""), device_cleanup: str = Form(""),
                            former_cleanup: str = Form(""), ssh_enforce: str = Form(""),
                            max_devices_per_student: int = Form(2), ports: str = Form("tcp:22"), extra: str = Form("[]"), extra_ssh: str = Form("[]")):
        who = admin(request)
        same_origin(request)
        port_list = [p.strip() for p in ports.split(",") if p.strip()]
        try:
            grants, ssh_rules = json.loads(extra or "[]"), json.loads(extra_ssh or "[]")
            assert isinstance(grants, list) and isinstance(ssh_rules, list), "both must be JSON lists"
            build_policy(_with_extra(enforcer.policy_input([]), grants, port_list, ssh_rules))
        except Exception as e:  # noqa: BLE001
            flash(request, f"Not saved: extra grants, extra SSH rules or ports invalid ({e}).", "err")
            return back("/settings")
        for key, val in (("enforce_policy", enforce_policy), ("device_cleanup", device_cleanup),
                         ("former_cleanup", former_cleanup), ("ssh_enforce", ssh_enforce)):
            db.set_setting(key, "1" if val else "0")
        db.set_setting("max_devices_per_student", str(max(1, max_devices_per_student)))
        db.set_setting("slot_ports", json.dumps(port_list or ["tcp:22"]))
        db.set_setting("extra_grants", json.dumps(grants))
        db.set_setting("extra_ssh", json.dumps(ssh_rules))
        db.audit(who, "settings-saved", f"policy={'on' if enforce_policy else 'off'} "
                                        f"cleanup={'on' if device_cleanup else 'dry-run'} "
                                        f"leavers={'on' if former_cleanup else 'dry-run'} "
                                        f"ssh={'on' if ssh_enforce else 'off'}")
        flash(request, "Settings saved. They apply on the next enforcement run (within a minute).")
        return back("/settings")

    @r.post("/settings/email-test")
    async def email_test(request: Request):
        who = admin(request)
        same_origin(request)
        if not mailer.enabled:
            flash(request, "Email is off: set it up as described under Email first.", "err")
            return back("/settings#email")

        def done(ok, detail):
            db.audit("mailer", "email-test-sent" if ok else "email-test-error", f"to {who}: {detail}")

        mailer.send([who], "Test email from the lab PC slot system",
                    f"This is a test from {cfg.public_url}{base}/settings, sent at {who}'s request.\n\n"
                    f"If it arrived, enrollment decisions will reach students the same way.\n\n{cfg.lab_name}", done)
        db.audit(who, "email-test", f"asked for a test email to {who}")
        flash(request, f"Sending a test email to {who}. Whether it was sent shows in the Log within a minute.")
        return back("/settings#email")

    @r.post("/settings/rotation")
    async def rotation_start(request: Request):
        who = admin(request)
        same_origin(request)
        f = await request.form()
        try:
            pc_id = int(f.get("pc") or 0)
            groups = [int(g) for g in f.getlist("group")]
            seconds = max(30, min(3600, int(f.get("seconds") or 90)))
            minutes = max(1, min(120, int(f.get("minutes") or 6)))
        except ValueError:
            flash(request, "Check the test rotation fields.", "err")
            return back("/settings")
        if not db.one("SELECT 1 FROM pcs WHERE id = ?", (pc_id,)) or not groups:
            flash(request, "Choose a PC and at least one group for the test rotation.", "err")
            return back("/settings")
        start = clock().replace(microsecond=0)
        db.set_setting("test_rotation", json.dumps({
            "pc_id": pc_id, "groups": groups, "seconds": seconds, "start": start.isoformat(),
            "end": (start + timedelta(minutes=minutes)).isoformat(), "by": who}))
        names = [g["name"] for gid in groups if (g := db.one("SELECT name FROM groups WHERE id = ?", (gid,)))]
        pc = db.one("SELECT name FROM pcs WHERE id = ?", (pc_id,))["name"]
        db.audit(who, "test-rotation-started", f"{pc}: {' -> '.join(names)} every {seconds}s for {minutes} min")
        flash(request, f"Test rotation on {pc} for {minutes} minutes. Its server's agent starts it within about 15 seconds.")
        return back("/")

    @r.post("/settings/rotation/stop")
    async def rotation_stop(request: Request):
        who = admin(request)
        same_origin(request)
        db.set_setting("test_rotation", "")
        db.audit(who, "test-rotation-stopped", "")
        flash(request, "Test rotation stopped; the normal routine applies again within about 15 seconds.")
        return back("/settings")

    @r.post("/settings/sheet/{action}")
    async def sheet_action(request: Request, action: str):
        who = admin(request)
        same_origin(request)
        if sync is None:
            raise HTTPException(404)
        if action == "sync":
            rep = await _in_thread(sync.pull)
            flash(request, (f"Read the Sheet: {len(rep.changes)} change(s), {len(rep.problems)} problem(s)."
                            if rep.ok else f"Could not read the Sheet: {rep.error}"), "ok" if rep.ok else "err")
        elif action == "restore":
            try:
                await _in_thread(lambda: sync.restore(who))
                flash(request, "Rewrote the Sheet from the last good copy.")
            except Exception as err:  # noqa: BLE001
                flash(request, f"Could not rewrite the Sheet: {err}", "err")
        else:
            raise HTTPException(404)
        return back("/settings")

    # -- audit ---------------------------------------------------------------------------------
    @r.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request):
        admin(request)
        return page(request, "audit.html", rows=db.q("SELECT * FROM audit ORDER BY id DESC LIMIT 300"))

    # -- enrollment: thesis groups apply, admins assign a PC and slots ---------------
    STUDENT_ID_RE = re.compile(r"^\d{8}$")
    MIN_MEMBERS, MAX_MEMBERS = 1, 5   # a thesis group: one to five students
    MAX_SUPERVISORS = 3    # the supervisor and up to two co-supervisors
    student_domain = cfg.student_domains[0] if cfg.student_domains else "students.example.edu"

    def student_address(raw: str) -> str:
        """A member's email as typed: the part before @<student domain>, or the whole address."""
        raw = raw.strip().lower()
        return f"{raw}@{student_domain}" if raw and "@" not in raw else raw

    def slot_text(weekday: int, half: str) -> str:
        return f"{WEEKDAYS[weekday]} {span(half, '-')}{' (overnight)' if half == 'night' else ''}"

    def group_assignment(group_id: int) -> dict:
        """PC, term and weekly slots a group holds (approved bookings), for status page and email."""
        rows = db.q("SELECT b.weekday, b.half, p.name AS pc_name, p.tag AS pc_tag, t.name AS term_name, "
                    "t.start_date, t.end_date FROM bookings b JOIN pcs p ON p.id = b.pc_id "
                    "JOIN terms t ON t.id = b.term_id WHERE b.group_id = ? AND b.status = 'approved' "
                    "ORDER BY t.start_date, p.name, b.weekday, b.half DESC", (group_id,))
        g = db.one("SELECT * FROM groups WHERE id = ?", (group_id,))
        ip = {t: n.get("ipv4", "") for t, n in node_index(reports(db, clock())).items()}
        pcs = {}
        for r_ in rows:
            pc = pcs.setdefault(r_["pc_name"], {"name": r_["pc_name"], "address": ip.get(r_["pc_tag"], ""),
                                                 "slots": [], "term": r_["term_name"],
                                                 "dates": f"{r_['start_date']} to {r_['end_date']}"})
            pc["slots"].append(slot_text(r_["weekday"], r_["half"]))
        return {"group": g, "pcs": list(pcs.values())}

    def notify(e: dict, subject: str, body: str) -> None:
        to = sorted({m["email"] for m in e["members"]} | {e["submitted_by"]})

        def done(ok, detail):
            db.audit("mailer", "email-sent" if ok else "email-error", f"enrollment #{e['id']}: {detail}")

        mailer.send(to, subject, body, done)

    def approval_email(e: dict, a: dict) -> str:
        lines = [f"Your thesis group \"{e['group_name']}\" is enrolled in the {cfg.lab_name} PC slot system.", ""]
        for pc in a["pcs"]:
            lines += [f"PC: {pc['name']}" + (f" (tailnet address {pc['address']})" if pc["address"] else ""),
                      f"Term: {pc['term']}, {pc['dates']}", "Your weekly slots:"]
            lines += [f"  - {sl}" for sl in pc["slots"]]
            lines.append("")
        account = a["group"]["linux_account"] if a["group"] else "your group account"
        lines += ["How to connect:",
                  "  1. Install Tailscale: https://tailscale.com/download",
                  f"  2. Run: tailscale login --login-server {cfg.public_url}",
                  f"     and sign in with your {cfg.org_name} Google account.",
                  f"  3. During your slot: ssh {account}@<PC address above>",
                  "     No SSH key or password: Tailscale checks who you are.",
                  "", "Outside your slots the PC is not reachable. Jobs running at the end of a slot are stopped,",
                  "so checkpoint your work.", "", cfg.lab_name]
        return "\n".join(lines)

    @r.get("/enroll", response_class=HTMLResponse)
    async def enroll_page(request: Request):
        email = request.session.get("email", "")
        if not email or not (db.is_admin(email) or is_student_email(email)):
            return page(request, "enroll_signin.html", oidc=oauth is not None, dev=cfg.dev_login,
                        student_domain=student_domain)
        mine = db.enrollments_for(email)
        for e in mine:
            e["assignment"] = group_assignment(e["group_id"]) if e["status"] == "approved" and e["group_id"] else None
        active = [e for e in mine if e["status"] in ("pending", "approved")]
        return page(request, "enroll.html", me=email, me_admin=db.is_admin(email), enrollments=mine, can_apply=not active,
                    min_members=MIN_MEMBERS, max_members=MAX_MEMBERS, max_supervisors=MAX_SUPERVISORS,
                    student_domain=student_domain,
                    form=request.session.pop("enroll_form", None))

    @r.post("/enroll")
    async def enroll_submit(request: Request):
        email = viewer(request)
        same_origin(request)
        f = await request.form()
        get = lambda k: (f.get(k) or "").strip()  # noqa: E731
        col = lambda k, i: ((f.getlist(k)[i] if i < len(f.getlist(k)) else "") or "").strip()  # noqa: E731
        members, supervisors, errors = [], [], []
        for i in range(MAX_MEMBERS):
            n, sid, em = col("m_name", i), col("m_id", i), student_address(col("m_email", i))
            if not (n or sid or em):
                continue
            members.append({"name": n, "student_id": sid, "email": em})
            if not n:
                errors.append(f"Member {i + 1}: give their full name.")
            if not STUDENT_ID_RE.match(sid):
                errors.append(f"Member {i + 1}: the student ID is 8 digits.")
            if em and (not EMAIL_RE.match(em) or not is_student_email(em)):
                errors.append(f"Member {i + 1}: use their {cfg.org_name} address (…@{student_domain}), "
                              f"not a personal one such as Gmail.")
            elif not em:
                errors.append(f"Member {i + 1}: give their {cfg.org_name} address.")
        if email not in {m["email"] for m in members}:
            errors.append("Include yourself as a member, with the email you signed in with.")
        if len(members) < MIN_MEMBERS:
            errors.append(f"A thesis group has {MIN_MEMBERS} to {MAX_MEMBERS} members: add your groupmates.")
        if len({m["email"] for m in members}) != len(members):
            errors.append("Each member's email must be different.")
        for i in range(MAX_SUPERVISORS):
            n, em = col("s_name", i), col("s_email", i).lower()
            if not (n or em):
                if i == 0:
                    errors.append("Give your supervisor's name and email.")
                continue
            supervisors.append({"name": n, "email": em})
            who = "Supervisor" if i == 0 else f"Co-supervisor {i}"
            if not n or not EMAIL_RE.match(em):
                errors.append(f"{who}: give a name and an email address.")
        for k, label in (("group_name", "group name"), ("thesis_title", "thesis title")):
            if not get(k):
                errors.append(f"Give the {label}.")
        if any(e["status"] in ("pending", "approved") for e in db.enrollments_for(email)):
            errors.append("You already have an enrollment that is waiting or approved.")
        if errors:
            for msg in errors:
                flash(request, msg, "err")
            request.session["enroll_form"] = {k: get(k) for k in ("group_name", "thesis_title", "hardware", "notes")} | {
                "members": members, "supervisors": supervisors}
            return back("/enroll")
        sup_names = [s["name"][:120] for s in supervisors]
        with db.tx():
            eid = db.conn.execute(
                "INSERT INTO enrolments(submitted_by, submitted_at, group_name, thesis_title, supervisor, "
                "co_supervisor, hardware, notes, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (email, now_iso(), get("group_name")[:120], get("thesis_title")[:300], sup_names[0],
                 ", ".join(sup_names[1:]), get("hardware")[:1000], get("notes")[:1000])).lastrowid
            db.conn.executemany("INSERT INTO enrolment_members(enrolment_id, position, name, student_id, email) "
                                "VALUES (?, ?, ?, ?, ?)", [(eid, i, m["name"][:120], m["student_id"], m["email"])
                                                         for i, m in enumerate(members)])
            db.conn.executemany("INSERT INTO enrolment_supervisors(enrolment_id, position, name, email) "
                                "VALUES (?, ?, ?, ?)", [(eid, i, s["name"][:120], s["email"][:200])
                                                      for i, s in enumerate(supervisors)])
        db.audit(email, "enrolment-submitted", f"#{eid} {get('group_name')} ({len(members)} members)")
        flash(request, "Enrollment sent. An admin will assign your group a PC; this page shows the result.")
        return back("/enroll")

    @r.post("/enroll/{eid}/withdraw")
    async def enroll_withdraw(request: Request, eid: int):
        email = viewer(request)
        same_origin(request)
        e = db.enrollment(eid)
        if e and e["submitted_by"] == email and e["status"] == "pending":
            db.x("UPDATE enrolments SET status = 'withdrawn', decided_by = ?, decided_at = ? WHERE id = ?",
                 (email, now_iso(), eid))
            db.audit(email, "enrolment-withdrawn", f"#{eid} {e['group_name']}")
            flash(request, "Enrollment withdrawn. You can send a new one.")
        return back("/enroll")

    @r.get("/enrollments", response_class=HTMLResponse)
    async def enrollments_page(request: Request):
        admin(request)
        rows = [db.enrollment(r_["id"]) for r_ in db.q("SELECT id FROM enrolments ORDER BY "
                                                      "status != 'pending', id DESC LIMIT 200")]
        return page(request, "enrollments.html", pending=[e for e in rows if e["status"] == "pending"],
                    decided=[e for e in rows if e["status"] != "pending"][:40], mailer_on=mailer.enabled)

    def slot_picker_ctx(term_id: int | None, pc_id: int | None) -> dict:
        taken = {}
        if term_id and pc_id:
            for b in db.q("SELECT b.weekday, b.half, g.name, g.short, g.slug, g.color FROM bookings b "
                          "JOIN groups g ON g.id = b.group_id WHERE b.term_id = ? AND b.pc_id = ? "
                          "AND b.status = 'approved'", (term_id, pc_id)):
                taken[(b["weekday"], b["half"])] = b
        return {"taken": taken, "picker_term": term_id, "picker_pc": pc_id}

    @r.get("/enrollments/{eid}", response_class=HTMLResponse)
    async def enrollment_page(request: Request, eid: int):
        admin(request)
        e = db.enrollment(eid)
        if not e:
            raise HTTPException(404)
        t, terms = pick_term(None)
        pcs = db.pcs()
        slug = re.sub(r"[^a-z0-9]+", "-", e["group_name"].lower()).strip("-")[:26] or f"group-{eid}"
        assignment = group_assignment(e["group_id"]) if e["group_id"] else None
        return page(request, "enrollment.html", e=e, terms=terms, term=t, pcs=pcs, suggested_slug=slug,
                    suggested_short=short_code(slug), assignment=assignment, mailer_on=mailer.enabled,
                    suggested_color=next_color([g["color"] for g in db.groups()]),
                    **slot_picker_ctx(t["id"] if t else None, pcs[0]["id"] if pcs else None))

    @r.get("/enrollments/{eid}/slots", response_class=HTMLResponse)
    async def enrollment_slots(request: Request, eid: int, term: int = 0, pc: int = 0):
        admin(request)
        return page(request, "_slot_picker.html", **slot_picker_ctx(term or None, pc or None))

    @r.post("/enrollments/{eid}/approve")
    async def enrollment_approve(request: Request, eid: int):
        who = admin(request)
        same_origin(request)
        e = db.enrollment(eid)
        if not e or e["status"] != "pending":
            flash(request, "This enrollment is no longer waiting.", "err")
            return back("/enrollments")
        f = await request.form()
        slug = (f.get("slug") or "").strip().lower()
        short = (f.get("short") or "").strip().upper() or short_code(slug)
        color = f.get("color") or next_color([g["color"] for g in db.groups()])
        term_id, pc_id = int(f.get("term") or 0), int(f.get("pc") or 0)
        slots = []
        for v in f.getlist("slot"):
            wd, _, half = v.partition("-")
            if wd.isdigit() and 0 <= int(wd) <= 6 and half in HALVES:
                slots.append((int(wd), half))
        problems = []
        if not SLUG_RE.match(slug) or not ACCOUNT_RE.match(f"grp-{slug}"[:32]):
            problems.append("Code: lowercase letters, digits and dashes, up to 27 characters.")
        if not re.match(r"^[A-Z0-9][A-Z0-9-]{0,7}$", short):
            problems.append("Short code: up to 8 capital letters, digits or dashes.")
        if not re.match(r"^#[0-9a-fA-F]{6}$", color):
            color = next_color([g["color"] for g in db.groups()])
        if not db.one("SELECT 1 FROM terms WHERE id = ?", (term_id,)) or not db.one("SELECT 1 FROM pcs WHERE id = ?", (pc_id,)):
            problems.append("Choose a term and a PC.")
        if not slots:
            problems.append("Tick at least one slot.")
        if db.one("SELECT 1 FROM groups WHERE slug = ?", (slug,)):
            problems.append(f"A group with code {slug} already exists; choose another code.")
        taken = slot_picker_ctx(term_id, pc_id)["taken"]
        clashes = [slot_text(*s_) for s_ in slots if s_ in taken]
        if clashes:
            problems.append("Already assigned: " + ", ".join(clashes) + ".")
        if sync is None:
            problems.append("Connect the Google Sheet first: groups and slots are written there.")
        if problems:
            for p_ in problems:
                flash(request, p_, "err")
            return back(f"/enrollments/{eid}")
        # The Sheet is the source of truth: write there, then read it back.
        t_ = db.one("SELECT name FROM terms WHERE id = ?", (term_id,))["name"]
        tag_ = db.one("SELECT tag FROM pcs WHERE id = ?", (pc_id,))["tag"]
        group_ = {"slug": slug, "name": e["group_name"], "short": short, "color": color,
                  "notes": f"{e['thesis_title']} · supervisor {e['supervisor']}",
                  "members": [m["email"] for m in e["members"]]}
        try:
            rep = await _in_thread(lambda: sync.assign(group_, t_, tag_, slots))
        except AssignError as err:
            flash(request, str(err), "err")
            return back(f"/enrollments/{eid}")
        row_ = db.one("SELECT id FROM groups WHERE slug = ?", (slug,))
        if not rep.ok or not row_:
            flash(request, "Written to the Sheet, but reading it back failed: " + (rep.error or "group missing")
                  + ". Check the Problems tab, then try again.", "err")
            return back(f"/enrollments/{eid}")
        gid = row_["id"]
        db.x("UPDATE enrolments SET status = 'approved', decided_by = ?, decided_at = ?, "
             "decision_note = ?, group_id = ? WHERE id = ?",
             (who, now_iso(), (f.get("note") or "").strip()[:500], gid, eid))
        pc_name = db.one("SELECT name FROM pcs WHERE id = ?", (pc_id,))["name"]
        db.audit(who, "enrolment-approved", f"#{eid} {e['group_name']} -> {pc_name}: "
                 + ", ".join(slot_text(*s_) for s_ in slots))
        e = db.enrollment(eid)
        notify(e, f"Enrolled: {e['group_name']} on {pc_name}", approval_email(e, group_assignment(gid)))
        flash(request, f"{e['group_name']} is enrolled on {pc_name} with {len(slots)} weekly slot(s)."
              + ("" if mailer.enabled else " Email is not configured, so tell the group to check their status page."))
        return back(f"/enrollments/{eid}")

    @r.post("/enrollments/{eid}/reject")
    async def enrollment_reject(request: Request, eid: int, note: str = Form("")):
        who = admin(request)
        same_origin(request)
        e = db.enrollment(eid)
        if not e or e["status"] != "pending":
            return back("/enrollments")
        db.x("UPDATE enrolments SET status = 'rejected', decided_by = ?, decided_at = ?, decision_note = ? "
             "WHERE id = ?", (who, now_iso(), note.strip()[:500], eid))
        db.audit(who, "enrolment-rejected", f"#{eid} {e['group_name']}: {note.strip()[:200]}")
        notify(e, f"Enrollment not approved: {e['group_name']}",
               f"Your enrollment for \"{e['group_name']}\" was not approved.\n\n"
               + (f"Reason: {note.strip()}\n\n" if note.strip() else "")
               + f"You can send a new enrollment at {cfg.public_url}{base}/enroll\n\n{cfg.lab_name}")
        flash(request, f"Rejected {e['group_name']}.")
        return back("/enrollments")

    # -- slot agents (one per Headscale server) ---------------------------------------
    def agent_server(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        name = server_for_token(db, auth[7:].strip() if auth.lower().startswith("bearer ") else "")
        if not name:
            raise HTTPException(401, "unknown agent token")
        return name

    @r.get("/api/agent/state")
    async def agent_state(request: Request):
        return server_slice(db, agent_server(request))

    @r.post("/api/agent/report")
    async def agent_report(request: Request):
        name = agent_server(request)
        body = await request.body()
        if len(body) > 1_000_000:
            raise HTTPException(413)
        try:
            rep = json.loads(body)
            assert isinstance(rep, dict)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "report must be a JSON object") from None
        store_report(db, name, rep)
        return {"ok": True}

    @r.get("/healthz")
    async def healthz():
        return {"ok": True, "version": version("labslots"),
                "servers": {r_["name"]: r_["last_seen"] for r_ in reports(db, clock())},
                **({"sheet_last_ok": db.setting("sheet_last_ok"), "sheet_error": db.setting("sheet_error")}
                   if sync else {})}

    app.include_router(r)

    @app.get("/")
    async def root():
        return RedirectResponse(f"{base}/", status_code=303)

    if cfg.run_scheduler and sync is not None:
        stop = threading.Event()
        app.state.stop = stop

        @app.on_event("startup")
        def _start():
            threading.Thread(target=run_sync_forever, args=(sync, cfg.interval, stop),
                             daemon=True, name="sheet-sync").start()

        @app.on_event("shutdown")
        def _stop():
            stop.set()

    return app


def _with_extra(inp, grants, ports, ssh_rules=()):
    inp.extra_grants = grants
    inp.extra_ssh = list(ssh_rules)
    inp.ports = ports or ["tcp:22"]
    return inp


async def _in_thread(fn):
    import anyio
    return await anyio.to_thread.run_sync(fn)


def run_sync_forever(sync: Sync, interval: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            rep = sync.pull()
            if not rep.ok:
                log.warning("sheet sync failed, keeping the last good copy: %s", rep.error)
        except Exception:  # noqa: BLE001
            log.exception("sheet sync crashed")
        stop.wait(interval)


def add_months(d: date, n: int) -> date:
    """First day of the month n months after d's month."""
    m = d.year * 12 + (d.month - 1) + n
    return date(m // 12, m % 12 + 1, 1)


def month_grid(first: date) -> dict:
    """A month as calendar weeks, Sunday first, padded with the neighbouring months' days."""
    lead = (first.weekday() + 1) % 7
    day = first - timedelta(days=lead)
    weeks = []
    while True:
        weeks.append([day + timedelta(days=i) for i in range(7)])
        day += timedelta(days=7)
        if day.month != first.month and day > first:
            break
    return {"first": first, "label": first.strftime("%B"), "year": first.year, "weeks": weeks}


def natural(name: str) -> list:
    """Sort key that puts pc-2 before pc-10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]
