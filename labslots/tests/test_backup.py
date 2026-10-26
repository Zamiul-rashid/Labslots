import sqlite3
from datetime import datetime, timedelta

from labslots.backup import backup_once


def test_backup_copies_and_prunes(tmp_path):
    src = tmp_path / "live.db"
    c = sqlite3.connect(src)
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (42)")
    c.commit()                                    # still open: the copy must work while the app runs
    dest = tmp_path / "backups"
    now = datetime(2026, 10, 22, 2, 30)
    old = dest / "live-20261001-0230.db"
    dest.mkdir()
    old.write_bytes(b"x")
    made = backup_once({"live": str(src), "missing": str(tmp_path / "nope.db")}, dest, 14, now)
    assert [m.name for m in made] == ["live-20261022-0230.db"]
    assert sqlite3.connect(made[0]).execute("SELECT x FROM t").fetchone() == (42,)
    assert not old.exists()                       # older than 14 days
    backup_once({"live": str(src)}, dest, 14, now + timedelta(days=1))
    assert len(list(dest.glob("*.db"))) == 2
