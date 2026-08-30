import difflib
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

    # Below this similarity ratio (difflib.SequenceMatcher, 0..1) a fuzzy
    # candidate is NOT trusted and get_mapping() falls back to asking the
    # user — chosen from real data: a genuine caption-phrasing drift for the
    # SAME anime ("Ми з тобою протилежності" vs a later post's "Ми з тобою
    # повні протилежності") scored ~0.89, while two actually-different
    # tracked titles scored ~0.38. 0.85 sits well above the gap.
    FUZZY_MATCH_THRESHOLD = 0.85

    def get_mapping(self, bad_title: str) -> str | None:
        """
        Returns the corrected title if a mapping exists. Tries an exact
        match first (fast path, the common case). Falls back to a fuzzy
        match against all known raw titles — AI-extracted captions for the
        SAME anime can drift slightly between posts (a later post adding or
        rewording a word the earlier one didn't have), which an exact match
        misses and would otherwise re-prompt the user for an already-known
        title every time the phrasing shifts slightly. Only trusts a fuzzy
        match above FUZZY_MATCH_THRESHOLD, to avoid conflating two
        genuinely different titles that just happen to look similar.
        """
        if not bad_title:
            return None
        normalized = bad_title.strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT official_title FROM mappings WHERE raw_title = ?",
                (normalized,)
            ).fetchone()
            if row:
                return row["official_title"]
            rows = conn.execute("SELECT raw_title, official_title FROM mappings").fetchall()

        target = " ".join(normalized.split()).lower()
        best_ratio = 0.0
        best_row = None
        for r in rows:
            candidate = " ".join((r["raw_title"] or "").split()).lower()
            ratio = difflib.SequenceMatcher(None, target, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_row = r

        if best_row and best_ratio >= self.FUZZY_MATCH_THRESHOLD:
            logger.info(
                f"Fuzzy title match: {bad_title!r} ~ {best_row['raw_title']!r} "
                f"(ratio={best_ratio:.2f}) -> {best_row['official_title']!r}"
            )
            return best_row["official_title"]
        return None

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
