import os
import sqlite3
import logging

logger = logging.getLogger(__name__)

DB_PATH = "sessions/mappings.db"


class TitleMapper:
    """
    Persistent raw-title -> official-title dictionary, backed by SQLite.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mappings (
                    raw_title      TEXT PRIMARY KEY,
                    official_title TEXT NOT NULL
                )
            """)

    def get_mapping(self, bad_title: str) -> str | None:
        """Returns the corrected title if a mapping exists (exact match on stripped raw title)."""
        if not bad_title:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT official_title FROM mappings WHERE raw_title = ?",
                (bad_title.strip(),)
            ).fetchone()
        return row["official_title"] if row else None

    def get_reverse_mapping(self, official_title: str) -> str | None:
        """
        Return any raw/localized title that maps to this official title, if
        one exists (arbitrary pick if multiple raw variants map to the same
        official title). Used to backfill a readable display name for
        records that predate display_title tracking.

        Compares with whitespace collapsed and case-folded on both sides —
        official titles were manually typed by the user on different
        occasions (once per confirmation prompt) and can differ by a stray
        double space or capitalization without being a "different" title.
        A plain SQL exact match (even with COLLATE NOCASE) misses those.
        """
        if not official_title:
            return None
        target = " ".join(official_title.split()).lower()
        with self._connect() as conn:
            rows = conn.execute("SELECT raw_title, official_title FROM mappings").fetchall()
        for row in rows:
            candidate = " ".join((row["official_title"] or "").split()).lower()
            if candidate == target:
                return row["raw_title"]
        return None

    def add_mapping(self, bad_title: str, correct_title: str):
        """Adds (or overwrites) a mapping."""
        if not bad_title or not correct_title:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO mappings (raw_title, official_title) VALUES (?, ?)",
                (bad_title.strip(), correct_title.strip())
            )
        logger.info(f"Added mapping: '{bad_title}' -> '{correct_title}'")


# Global instance
mapper = TitleMapper()
