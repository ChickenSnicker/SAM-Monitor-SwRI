"""
history.py — Deduplication store for seen SAM.gov opportunities.

Two interchangeable backends:

  * JSONHistory   — a single human-readable .json file. Preferred for
                    GitHub Actions, where the file is committed back to the
                    repo so it survives the ephemeral runner.
  * SQLiteHistory — a local .db file. Better for long-lived local runs with
                    very large histories.

Both expose the same interface, so monitor.py does not care which is in use:
    is_new(notice_id)            -> bool
    filter_new(opportunities)    -> list[dict]
    mark_seen(opportunities, by) -> None
    get_last_run()               -> date | None
    set_last_run(date)           -> None
    stats()                      -> dict
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def _resolve(path: str) -> str:
    """Make a path absolute relative to this script's directory."""
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, path)
    return path


def _record_from(op: Dict, matched_by: str, now_iso: str) -> Dict:
    """Normalize a SAM.gov opportunity into a compact history record."""
    return {
        "notice_id": op.get("noticeId", ""),
        "title": op.get("title", ""),
        "agency": op.get("fullParentPathName", ""),
        "naics_code": op.get("naicsCode", ""),
        "posted_date": op.get("postedDate", ""),
        # NOTE: 'reponseDeadLine' is misspelled in the SAM.gov API itself.
        "response_deadline": op.get("reponseDeadLine", ""),
        "ui_link": op.get("uiLink", ""),
        "matched_by": matched_by,
        "first_seen_at": now_iso,
    }


# ─────────────────────────────────────────────────────────────
#  JSON backend
# ─────────────────────────────────────────────────────────────

class JSONHistory:
    """
    Stores seen opportunities in a single JSON file.

    File shape:
        {
          "last_run": "2026-09-21",
          "opportunities": { "<notice_id>": { ...record... }, ... }
        }
    """

    def __init__(self, path: str, retention_days: int = 365):
        self.path = _resolve(path)
        self.retention_days = retention_days
        self._data = self._load()

    # ---- persistence ----

    def _load(self) -> Dict:
        if not os.path.exists(self.path):
            logger.info("No history file at %s — starting fresh.", self.path)
            return {"last_run": None, "opportunities": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("last_run", None)
            data.setdefault("opportunities", {})
            logger.debug(
                "Loaded %d historical opportunities from %s",
                len(data["opportunities"]), self.path,
            )
            return data
        except (json.JSONDecodeError, OSError) as exc:
            # Never hard-fail on a corrupt history: back it up and start clean,
            # otherwise a bad file would block the monitor indefinitely.
            logger.error("Could not read history file (%s). Backing up and starting fresh.", exc)
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return {"last_run": None, "opportunities": {}}

    def _save(self) -> None:
        # Write to a temp file then replace, so an interrupted run cannot
        # leave behind a truncated history file.
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)
        logger.debug("Saved history (%d records) to %s", len(self._data["opportunities"]), self.path)

    # ---- interface ----

    def is_new(self, notice_id: str) -> bool:
        return bool(notice_id) and notice_id not in self._data["opportunities"]

    def filter_new(self, opportunities: List[Dict]) -> List[Dict]:
        return [op for op in opportunities if self.is_new(op.get("noticeId", ""))]

    def mark_seen(self, opportunities: List[Dict], matched_by: str = "") -> None:
        now_iso = datetime.utcnow().isoformat(timespec="seconds")
        added = 0
        for op in opportunities:
            nid = op.get("noticeId", "")
            if not nid or nid in self._data["opportunities"]:
                continue
            self._data["opportunities"][nid] = _record_from(op, matched_by, now_iso)
            added += 1
        if added:
            self._prune()
            self._save()
        logger.debug("Recorded %d new opportunities.", added)

    def get_last_run(self) -> Optional[date]:
        raw = self._data.get("last_run")
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    def set_last_run(self, run_date: date) -> None:
        self._data["last_run"] = run_date.isoformat()
        self._save()

    def stats(self) -> Dict:
        opps = self._data["opportunities"]
        seen_times = [r.get("first_seen_at", "") for r in opps.values() if r.get("first_seen_at")]
        return {
            "total_seen": len(opps),
            "oldest": min(seen_times) if seen_times else None,
            "newest": max(seen_times) if seen_times else None,
            "last_run": self._data.get("last_run"),
        }

    # ---- housekeeping ----

    def _prune(self) -> None:
        """
        Drop records older than retention_days to keep the file small.

        Retention defaults to a year because dropping a record makes that
        opportunity eligible to alert again if it is still open and matching.
        """
        if not self.retention_days or self.retention_days <= 0:
            return
        cutoff = (datetime.utcnow() - timedelta(days=self.retention_days)).isoformat()
        before = len(self._data["opportunities"])
        self._data["opportunities"] = {
            nid: rec
            for nid, rec in self._data["opportunities"].items()
            if rec.get("first_seen_at", "") >= cutoff
        }
        dropped = before - len(self._data["opportunities"])
        if dropped:
            logger.info("Pruned %d history records older than %d days.", dropped, self.retention_days)


# ─────────────────────────────────────────────────────────────
#  SQLite backend
# ─────────────────────────────────────────────────────────────

class SQLiteHistory:
    """Stores seen opportunities in a local SQLite database."""

    CREATE_OPPS = """
    CREATE TABLE IF NOT EXISTS seen_opportunities (
        notice_id         TEXT PRIMARY KEY,
        title             TEXT,
        agency            TEXT,
        naics_code        TEXT,
        posted_date       TEXT,
        response_deadline TEXT,
        ui_link           TEXT,
        matched_by        TEXT,
        first_seen_at     TEXT NOT NULL
    );
    """
    CREATE_META = """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """

    def __init__(self, db_path: str, retention_days: int = 365):
        self.path = _resolve(db_path)
        self.retention_days = retention_days
        with self._conn() as conn:
            conn.execute(self.CREATE_OPPS)
            conn.execute(self.CREATE_META)
        logger.debug("SQLite history ready at %s", self.path)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def is_new(self, notice_id: str) -> bool:
        if not notice_id:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_opportunities WHERE notice_id = ?", (notice_id,)
            ).fetchone()
        return row is None

    def filter_new(self, opportunities: List[Dict]) -> List[Dict]:
        return [op for op in opportunities if self.is_new(op.get("noticeId", ""))]

    def mark_seen(self, opportunities: List[Dict], matched_by: str = "") -> None:
        now_iso = datetime.utcnow().isoformat(timespec="seconds")
        rows = []
        for op in opportunities:
            if not op.get("noticeId"):
                continue
            r = _record_from(op, matched_by, now_iso)
            rows.append((
                r["notice_id"], r["title"], r["agency"], r["naics_code"],
                r["posted_date"], r["response_deadline"], r["ui_link"],
                r["matched_by"], r["first_seen_at"],
            ))
        if not rows:
            return
        with self._conn() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO seen_opportunities
                   (notice_id, title, agency, naics_code, posted_date,
                    response_deadline, ui_link, matched_by, first_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        self._prune()
        logger.debug("Recorded %d new opportunities.", len(rows))

    def get_last_run(self) -> Optional[date]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'last_run'").fetchone()
        if not row or not row[0]:
            return None
        try:
            return date.fromisoformat(row[0])
        except ValueError:
            return None

    def set_last_run(self, run_date: date) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('last_run', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (run_date.isoformat(),),
            )

    def stats(self) -> Dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM seen_opportunities").fetchone()[0]
            oldest = conn.execute("SELECT MIN(first_seen_at) FROM seen_opportunities").fetchone()[0]
            newest = conn.execute("SELECT MAX(first_seen_at) FROM seen_opportunities").fetchone()[0]
        last_run = self.get_last_run()
        return {
            "total_seen": total,
            "oldest": oldest,
            "newest": newest,
            "last_run": last_run.isoformat() if last_run else None,
        }

    def _prune(self) -> None:
        if not self.retention_days or self.retention_days <= 0:
            return
        cutoff = (datetime.utcnow() - timedelta(days=self.retention_days)).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM seen_opportunities WHERE first_seen_at < ?", (cutoff,)
            )
            if cur.rowcount > 0:
                logger.info("Pruned %d history records older than %d days.", cur.rowcount, self.retention_days)


# ─────────────────────────────────────────────────────────────
#  Factory
# ─────────────────────────────────────────────────────────────

def get_history(cfg: dict):
    """Build the history backend specified by config.storage.backend."""
    storage = cfg.get("storage", {})
    backend = str(storage.get("backend", "json")).strip().lower()
    retention = storage.get("retention_days", 365)

    if backend == "sqlite":
        return SQLiteHistory(storage.get("db_path", "history.db"), retention_days=retention)
    if backend == "json":
        return JSONHistory(storage.get("json_path", "history.json"), retention_days=retention)
    raise ValueError(f"Unknown storage.backend '{backend}' — expected 'json' or 'sqlite'.")
