from conftest import seed

from labslots.sheets import FakeSheets, a1, col_letter, parse_a1, sheet_id_from
from labslots.sync import GROUPS, PCS, PROBLEMS, ROUTINE_HEADER, TERMS, Sync, routine_tab

R = routine_tab("T")


def booked(db):
    return sorted((r["pc"], r["weekday"], r["half"], r["slug"]) for r in db.q(
        "SELECT p.name AS pc, b.weekday, b.half, g.slug FROM bookings b JOIN pcs p ON p.id = b.pc_id "
        "JOIN groups g ON g.id = b.group_id WHERE b.status = 'approved'"))


def col(wd, half):
    return 1 + wd * 2 + (half == "night")


def problems(sheet):
    return [r for r in sheet.tabs[PROBLEMS][1:] if len(r) > 2]


def test_a1_helpers():
    assert col_letter(0) == "A" and col_letter(25) == "Z" and col_letter(26) == "AA"
    assert a1("Routine T", 2, 1) == "'Routine T'!B3"
    assert parse_a1("'It''s'!AB10") == ("It's", 9, 27)
    assert sheet_id_from("https://docs.google.com/spreadsheets/d/abc_DEF-1/edit#gid=0") == "abc_DEF-1"


def test_first_pull_builds_the_sheet_from_sqlite(db):
    seed(db)
    sheet = FakeSheets({"Sheet1": []})
    rep = Sync(db, sheet).pull()
    assert rep.ok and rep.seeded and not rep.problems
    assert rep.changes == ["updated group thesis-a", "updated group thesis-b"]  # seed() leaves short codes blank
    assert "Sheet1" not in sheet.tabs and {GROUPS, PCS, TERMS, R, PROBLEMS} <= set(sheet.tabs)
    assert sheet.tabs[R][0] == ROUTINE_HEADER
    assert sheet.tabs[R][1][col(0, "day")] == "TH-A" and sheet.tabs[R][1][col(0, "night")] == "TH-B"
    assert sheet.tabs[GROUPS][1][4] == "alice@students.example.edu"
    assert any("setDataValidation" in q for q in sheet.requests)
    before = booked(db)
    assert Sync(db, sheet).pull().changes == [] and booked(db) == before   # round trip is stable


def test_sheet_edits_flow_into_sqlite(db):
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    s.pull()
    n0 = db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'sheet-sync'")["n"]
    sheet.set(R, 1, col(2, "day"), "thesis-b")       # new slot
    sheet.set(R, 1, col(0, "night"), "")             # freed
    sheet.set(GROUPS, 1, 4, "alice@students.example.edu\ncarol@students.example.edu")
    sheet.set(PCS, 2, 2, "Yes")                      # pc-43 account switching on
    sheet.write({a1(TERMS, 2, 0): [["Spring 2027", "2027-02-01", "2027-05-31"]]})
    rep = s.pull()
    assert rep.ok and not rep.problems, rep.problems
    assert booked(db) == [("pc-41", 0, "day", "thesis-a"), ("pc-41", 2, "day", "thesis-b")]
    assert sorted(db.members()[1]) == ["alice@students.example.edu", "carol@students.example.edu"]
    assert db.one("SELECT ssh_enabled FROM pcs WHERE name = 'pc-43'")["ssh_enabled"] == 1
    assert db.one("SELECT 1 FROM terms WHERE name = 'Spring 2027'")
    assert routine_tab("Spring 2027") in sheet.tabs          # a new term gets its routine tab
    assert db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'sheet-sync'")["n"] == n0 + 1


def test_bad_cell_keeps_last_good_value(db):
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    sheet.set(R, 1, col(0, "day"), "thesis-zzz")     # typo over an existing booking
    sheet.set(R, 1, col(3, "day"), "nobody")         # typo in a free slot
    sheet.set(R, 1, col(4, "day"), "thesis-b")       # a good edit in the same pull still applies
    rep = s.pull()
    assert ("pc-41", 0, "day", "thesis-a") in booked(db)
    assert ("pc-41", 4, "day", "thesis-b") in booked(db)
    assert not any(b[1] == 3 for b in booked(db))
    got = {(p[1], p[3]) for p in problems(sheet)}
    assert ("thesis-zzz", "Kept thesis-a") in got and ("nobody", "Slot left free") in got
    assert len(rep.problems) == 2 and db.setting("sheet_problem_count") == "2"
    sheet.set(R, 1, col(0, "day"), "thesis-a")
    sheet.set(R, 1, col(3, "day"), "")
    s.pull()
    assert problems(sheet) == []


def test_bad_rows_and_short_codes(db):
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    sheet.write({a1(GROUPS, 3, 0): [["Bad Code!", "X"]], a1(GROUPS, 1, 3): [["red"]],
                 a1(TERMS, 1, 1): [["not a date"]], a1(PCS, 1, 2): [["maybe"]]})
    sheet.set(R, 1, col(5, "night"), "TH-B")          # short code works too
    s.pull()
    assert db.one("SELECT color FROM groups WHERE slug = 'thesis-a'")["color"] == "#ffcccc"
    assert db.one("SELECT start_date FROM terms WHERE name = 'T'")["start_date"] == "2026-10-01"
    assert db.one("SELECT ssh_enabled FROM pcs WHERE name = 'pc-41'")["ssh_enabled"] == 1
    assert ("pc-41", 5, "night", "thesis-b") in booked(db)
    assert len(problems(sheet)) == 4


def test_changed_header_or_mass_delete_is_held(db):
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    before = booked(db)
    sheet.tabs[R][0][1] = "Monday"                    # header edited: tab ignored
    sheet.tabs[GROUPS] = []                           # tab emptied: ignored
    s.pull()
    assert booked(db) == before and len(db.groups()) == 2
    assert len(problems(sheet)) == 2
    for i in range(3, 9):                             # 6 groups, then delete 5 at once
        db.x("INSERT INTO groups(name, slug, linux_account) VALUES (?, ?, ?)", (f"G{i}", f"g{i}", f"grp-g{i}"))
    sheet.tabs[R][0][1] = ROUTINE_HEADER[1]
    s.restore("boss@example.edu")
    assert len(sheet.tabs[GROUPS]) == 9
    sheet.tabs[GROUPS] = sheet.tabs[GROUPS][:3]
    s.pull()
    assert len(db.groups()) == 8 and any("accident" in p[2] for p in problems(sheet))


def test_google_down_keeps_enforcing(db):
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    before = booked(db)
    sheet.fail = True
    rep = s.pull()
    assert not rep.ok and "503" in rep.error and booked(db) == before
    assert db.setting("sheet_error") and db.one("SELECT 1 FROM audit WHERE action = 'sheet-unreachable'")
    s.pull()   # a second failure is not logged again
    assert db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'sheet-unreachable'")["n"] == 1
    sheet.fail = False
    assert s.pull().ok and not db.setting("sheet_error")


def test_assign_writes_to_the_sheet_and_checks_it_fresh(db):
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    g = {"slug": "mars", "name": "MarsVision AI", "short": "MARS", "color": "#ffe08a", "notes": "",
         "members": ["ridwan@students.example.edu", "x@students.example.edu"]}
    sheet.set(R, 2, col(3, "day"), "thesis-a")        # someone just filled pc-43 Thu day in the Sheet
    try:
        s.assign(g, "T", "tag:pc-43", [(3, "day")])
        raise AssertionError("expected a clash")
    except Exception as e:  # noqa: BLE001
        assert "Thu day (thesis-a)" in str(e)
    rep = s.assign(g, "T", "tag:pc-43", [(2, "day"), (4, "night")])
    assert rep.ok
    assert ("pc-43", 2, "day", "mars") in booked(db) and ("pc-43", 4, "night", "mars") in booked(db)
    assert sorted(db.members()[db.one("SELECT id FROM groups WHERE slug = 'mars'")["id"]]) == g["members"]


def test_cells_show_short_codes_and_follow_a_renamed_short_code(db):
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    sheet.set(R, 1, col(3, "day"), "thesis-b")       # typed the long code
    s.pull()
    assert sheet.tabs[R][1][col(3, "day")] == "TH-B" and ("pc-41", 3, "day", "thesis-b") in booked(db)
    sheet.set(GROUPS, 2, 2, "BEE")                    # thesis-b's short code renamed
    s.pull()
    assert ("pc-41", 3, "day", "thesis-b") in booked(db) and problems(sheet) == []
    assert sheet.tabs[R][1][col(3, "day")] == "BEE" and sheet.tabs[R][1][col(0, "night")] == "BEE"


def test_old_one_line_headers_are_accepted_and_upgraded(db):
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    sheet.tabs[R][0] = [h.split("\n")[0] for h in ROUTINE_HEADER]   # "Mon day", as Sheets made before
    before = booked(db)
    s.pull()
    assert booked(db) == before and sheet.tabs[R][0] == ROUTINE_HEADER


def test_style_runs_once_until_something_changes(db):
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    styled = [q for q in sheet.requests if "addConditionalFormatRule" in q]
    assert any(q["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"].get("values", [{}])[0]
               .get("userEnteredValue") == "TH-A" for q in styled)
    n = len(sheet.requests)
    s.pull()
    assert len(sheet.requests) == n                   # nothing changed: no restyle
    sheet.set(GROUPS, 1, 3, "#b8e6b0")                # a colour changed: restyle
    s.pull()
    assert len(sheet.requests) > n


def test_read_me_is_kept_current(db):
    from labslots.sync import README, README_TEXT
    seed(db)
    sheet = FakeSheets()
    s = Sync(db, sheet)
    s.pull()
    sheet.tabs[README] = [["old text"]]
    s.pull()
    assert [r[0] for r in sheet.tabs[README] if r] == [r[0] for r in README_TEXT]
    writes = sheet.writes
    s.pull()
    assert sheet.writes == writes                     # unchanged: not rewritten every pull


def test_enrolments_tab_from_before_is_renamed(db):
    from labslots.sync import ENROLLMENTS
    seed(db)
    sync = Sync(db, FakeSheets())
    sync.pull()
    sheet = sync.sheet
    sheet.tabs = {("Enrolments" if t == ENROLLMENTS else t): rows for t, rows in sheet.tabs.items()}  # an older Sheet
    rows_before = sheet.tabs["Enrolments"]
    rep = sync.pull()
    assert rep.ok, rep.error
    assert "Enrolments" not in sheet.tabs and sheet.tabs[ENROLLMENTS] == rows_before   # same tab, new name
