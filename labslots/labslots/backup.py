"""Nightly copies of the SQLite databases, kept for a number of days.

Uses SQLite's online backup API, so a copy is consistent even while the website,
the agent or Headscale is writing. Run by the `backup` service in docker-compose.yml:

    BACKUP_SOURCES="labslots=/src/labslots/labslots.db,agent=/src/agent/agent.db" python -m labslots.backup
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("labslots.backup")


def backup_once(sources: dict[str, str], dest: Path, keep_days: int, now: datetime | None = None) -> list[Path]:
    now = now or datetime.now()
    dest.mkdir(parents=True, exist_ok=True)
    made = []
    for name, path in sources.items():
        if not Path(path).exists():
            log.warning("skipping %s: %s does not exist", name, path)
            continue
        out = dest / f"{name}-{now.strftime('%Y%m%d-%H%M')}.db"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(out, 0o600)
        made.append(out)
        log.info("backed up %s to %s (%d KB)", name, out, out.stat().st_size // 1024)
    cutoff = now - timedelta(days=keep_days)
    for f in dest.glob("*.db"):
        try:
            stamp = datetime.strptime("-".join(f.stem.rsplit("-", 2)[-2:]), "%Y%m%d-%H%M")
        except ValueError:
            continue
        if stamp < cutoff:
            f.unlink()
            log.info("removed old backup %s", f.name)
    return made


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    e = os.environ.get
    sources = dict(item.split("=", 1) for item in e("BACKUP_SOURCES", "").split(",") if "=" in item)
    dest, keep = Path(e("BACKUP_DIR", "/backups")), int(e("BACKUP_KEEP_DAYS", "14"))
    at = e("BACKUP_AT", "02:30")  # local time each night
    while True:
        try:
            backup_once(sources, dest, keep)
            (dest / ".last-ok").write_text(datetime.now().isoformat(timespec="seconds"))
        except Exception:  # noqa: BLE001
            log.exception("backup failed")
        h, m = (int(x) for x in at.split(":"))
        now = datetime.now()
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep((nxt - now).total_seconds())


if __name__ == "__main__":
    main()
