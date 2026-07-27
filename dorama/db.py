import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = "sessions/dorama.db"
MAX_AGE_DAYS = 182  # ~6 months


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    Path("sessions").mkdir(exist_ok=True)
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS series (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       INTEGER NOT NULL,
                title         TEXT    NOT NULL,
                base_url      TEXT    NOT NULL,
                last_season   INTEGER NOT NULL DEFAULT 1,
                last_episode  INTEGER NOT NULL DEFAULT 0,
                started_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                active        INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id     INTEGER NOT NULL REFERENCES series(id),
                season        INTEGER NOT NULL,
                episode       INTEGER NOT NULL,
                downloaded_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)
        # Migration: `category` distinguishes dorama (-> DORAMA_PATH) from
        # anime (-> DOWNLOAD_PATH) tracking. Older DBs predate this column.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(series)").fetchall()}
        if "category" not in cols:
            conn.execute("ALTER TABLE series ADD COLUMN category TEXT NOT NULL DEFAULT 'dorama'")
    logger.info("Dorama DB initialized.")


def add_series(chat_id: int, title: str, url: str, category: str = "dorama") -> int:
    """
    Add a new series/title to track.
    `url` — the page used to list available episodes (per-episode/serial root/TG topic).
    `category` — "dorama" (-> DORAMA_PATH) or "anime" (-> DOWNLOAD_PATH).
    Stored in the base_url column. Episode tracking is driven by the `episodes` table.
    """
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO series (chat_id, title, base_url, category) VALUES (?, ?, ?, ?)",
            (chat_id, title, url, category)
        )
        return cur.lastrowid


def get_downloaded_set(series_id: int) -> set[tuple[int, int]]:
    """Return {(season, episode), ...} already downloaded for this series."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT season, episode FROM episodes WHERE series_id = ?", (series_id,)
        ).fetchall()
    return {(r["season"], r["episode"]) for r in rows}


def get_series_by_id(series_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM series WHERE id = ?", (series_id,)
        ).fetchone()


def get_active_series() -> list[sqlite3.Row]:
    cutoff = _cutoff_date()
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM series WHERE active = 1 AND started_at > ? ORDER BY id",
            (cutoff,)
        ).fetchall()


def get_series_by_chat(chat_id: int, category: str | None = None) -> list[sqlite3.Row]:
    """If `category` is given, only return series of that category (dorama/anime)."""
    with _connect() as conn:
        if category is None:
            return conn.execute(
                "SELECT * FROM series WHERE chat_id = ? AND active = 1 ORDER BY id DESC",
                (chat_id,)
            ).fetchall()
        return conn.execute(
            "SELECT * FROM series WHERE chat_id = ? AND category = ? AND active = 1 ORDER BY id DESC",
            (chat_id, category)
        ).fetchall()


def stop_series(series_id: int):
    with _connect() as conn:
        conn.execute("UPDATE series SET active = 0 WHERE id = ?", (series_id,))


def record_episode(series_id: int, season: int, episode: int):
    """Update last downloaded episode and insert episode record."""
    with _connect() as conn:
        conn.execute(
            "UPDATE series SET last_season = ?, last_episode = ? WHERE id = ?",
            (season, episode, series_id)
        )
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode) VALUES (?, ?, ?)",
            (series_id, season, episode)
        )


def seed_downloaded_episodes(series_id: int, episodes: set[tuple[int, int]]):
    """
    Mark episodes as already downloaded WITHOUT this being a fresh download —
    used to import files found on disk before tracking started (e.g. earlier
    manual Normal/Batch mode downloads), so the checker won't re-fetch them.
    Skips episodes already recorded; updates last_season/last_episode to the
    highest (season, episode) found.
    """
    if not episodes:
        return
    with _connect() as conn:
        already = {
            (r["season"], r["episode"])
            for r in conn.execute(
                "SELECT season, episode FROM episodes WHERE series_id = ?", (series_id,)
            ).fetchall()
        }
        for season, episode in episodes - already:
            conn.execute(
                "INSERT INTO episodes (series_id, season, episode) VALUES (?, ?, ?)",
                (series_id, season, episode)
            )
        max_season, max_episode = max(episodes)
        conn.execute(
            "UPDATE series SET last_season = ?, last_episode = ? WHERE id = ?",
            (max_season, max_episode, series_id)
        )


def deactivate_expired():
    """Deactivate series older than MAX_AGE_DAYS."""
    cutoff = _cutoff_date()
    with _connect() as conn:
        n = conn.execute(
            "UPDATE series SET active = 0 WHERE active = 1 AND started_at <= ?",
            (cutoff,)
        ).rowcount
    if n:
        logger.info(f"Deactivated {n} expired dorama series.")


def _cutoff_date() -> str:
    return (datetime.now() - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
