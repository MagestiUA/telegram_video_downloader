import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

from analyzer.mapper import mapper

logger = logging.getLogger(__name__)

DB_PATH = "sessions/anime.db"
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
            CREATE TABLE IF NOT EXISTS caption_cache (
                chat        TEXT    NOT NULL,
                message_id  INTEGER NOT NULL,
                season      INTEGER NOT NULL,
                episode     INTEGER NOT NULL,
                PRIMARY KEY (chat, message_id)
            );
        """)
        # Migration: `category` exists for legacy rows only (an earlier,
        # since-removed tracking mode used a different value here). "anime"
        # is the only category ever created going forward.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(series)").fetchall()}
        if "category" not in cols:
            conn.execute("ALTER TABLE series ADD COLUMN category TEXT NOT NULL DEFAULT 'anime'")
        # Migration: `display_title` is the localized/raw caption title shown
        # to users (readable), while `title` stays the official Romaji name
        # used for folder/file naming. Older DBs predate this column.
        if "display_title" not in cols:
            conn.execute("ALTER TABLE series ADD COLUMN display_title TEXT")
        # Migration: `total_episodes` — the season's known total episode
        # count, resolved once via DeepSeek (see analyzer.ai_cleaner.
        # extract_total_episodes) once enough episodes are downloaded, so
        # the checker can auto-stop tracking on the actual last episode
        # instead of relying solely on the "N з N" caption pattern, which
        # many channels never post. 0 means "not yet resolved" — chosen
        # over NULL so every read/comparison can treat it as a plain int.
        if "total_episodes" not in cols:
            conn.execute("ALTER TABLE series ADD COLUMN total_episodes INTEGER NOT NULL DEFAULT 0")
        else:
            # An earlier version of this column allowed NULL — normalize
            # any such rows to 0 so callers never have to special-case None.
            conn.execute("UPDATE series SET total_episodes = 0 WHERE total_episodes IS NULL")
    logger.info("Anime tracking DB initialized.")


def add_series(chat_id: int, title: str, url: str, category: str = "anime",
               display_title: str | None = None) -> int:
    """
    Add a new series/title to track.
    `url` — the page used to list available episodes (per-episode/serial root/TG topic).
    `category` — always "anime" for anything created going forward; kept as a
    column for legacy rows from an earlier, since-removed tracking mode.
    `title` — official Romaji title, used for folder/file naming.
    `display_title` — localized/raw title shown in bot messages (falls back
    to `title` if not given).
    Stored in the base_url column. Episode tracking is driven by the `episodes` table.
    """
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO series (chat_id, title, base_url, category, display_title) VALUES (?, ?, ?, ?, ?)",
            (chat_id, title, url, category, display_title or title)
        )
        return cur.lastrowid


def find_active_series_by_title(title: str, category: str) -> sqlite3.Row | None:
    """
    Case-insensitive lookup for an already-tracked active series with the
    same (official) title in this category — used to reject duplicate adds.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM series WHERE active = 1 AND category = ? AND title = ? COLLATE NOCASE",
            (category, title.strip())
        ).fetchone()


def set_display_title(series_id: int, display_title: str):
    """Backfill/update the localized display name for an existing series row."""
    with _connect() as conn:
        conn.execute(
            "UPDATE series SET display_title = ? WHERE id = ?",
            (display_title, series_id)
        )


def resolve_display_title(series: sqlite3.Row) -> str:
    """
    Return the localized display name for a series row shown in bot
    messages. If this row predates display_title tracking (added before that
    column existed), backfill it via mapper.db's reverse lookup (raw
    localized title -> official title) and persist the result so this only
    ever needs to happen once. Used by BOTH the /anime list rendering and
    checker.py's download notifications, so either one being viewed first
    fixes it for both.
    """
    display = series["display_title"]
    if display:
        return display
    title = series["title"]
    reverse = mapper.get_reverse_mapping(title)
    if reverse:
        set_display_title(series["id"], reverse)
        logger.info(f"Backfilled display_title: {title!r} -> {reverse!r}")
        return reverse
    logger.warning(
        f"No reverse mapping found for title={title!r} (repr shown to catch "
        f"invisible whitespace/encoding mismatches vs mappings.db)."
    )
    return title


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
    """If `category` is given, only return series of that category."""
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


def get_all_active_series(category: str) -> list[sqlite3.Row]:
    """
    All active series of a category, regardless of who added them (chat_id).
    Used for the shared /anime list — every authorized user tracks the same
    pool of titles and gets notified of every download, so the list and the
    ability to stop a title must be shared too, not scoped to whoever added it.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM series WHERE category = ? AND active = 1 ORDER BY id DESC",
            (category,)
        ).fetchall()


def get_recent_series() -> list[sqlite3.Row]:
    """
    All series started within MAX_AGE_DAYS, active OR stopped — unlike
    get_active_series()/get_all_active_series(), this INCLUDES finished/
    manually-stopped titles too. Used by the "Виправити тайтл" menu, since a
    wrongly-downloaded episode might belong to a title that already finished
    airing (and so is no longer "active") but is still worth fixing.
    """
    cutoff = _cutoff_date()
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM series WHERE started_at > ? ORDER BY id DESC",
            (cutoff,)
        ).fetchall()


def get_episodes(series_id: int) -> list[sqlite3.Row]:
    """All downloaded-episode records for a series, oldest first."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM episodes WHERE series_id = ? ORDER BY season, episode",
            (series_id,)
        ).fetchall()


def delete_episode(series_id: int, season: int, episode: int):
    """
    Remove an episode's DB record — used when fixing a wrongly-downloaded
    episode, so the next check cycle treats it as not-yet-downloaded and
    picks it up again automatically (or it's redownloaded manually).
    """
    with _connect() as conn:
        conn.execute(
            "DELETE FROM episodes WHERE series_id = ? AND season = ? AND episode = ?",
            (series_id, season, episode)
        )


def set_base_url(series_id: int, new_url: str):
    """
    Re-anchor a single series to a brand-new source URL, keeping its id,
    title, display_title, and downloaded-episode history intact. Needed when
    a source channel doesn't just rename (see rebase_channel_username) but
    gets rebuilt/reposted from scratch — old forum-topic anchor message IDs
    become permanently invalid even after fixing the username, since the
    message numbering itself was reset. The caller re-points the series at
    whatever the CURRENT anchor message for that title is; the checker picks
    up new episodes from there without re-downloading what's already in the
    episodes table.
    """
    with _connect() as conn:
        conn.execute("UPDATE series SET base_url = ? WHERE id = ?", (new_url, series_id))


def rebase_channel_username(old_username: str, new_username: str) -> int:
    """
    Bulk-fix every series whose base_url still points at a shared
    "media library" channel's OLD public @username after the channel owner
    renames it in Telegram. A username change breaks every stored
    t.me/{old_username}/{msg_id} link outright — Telegram frees the old name
    for anyone else to claim, while the channel itself (and its numeric
    chat_id) is unaffected. Private per-title channels (t.me/+invite-hash)
    are unaffected by this — they're tracked by numeric chat_id, not a
    username, so they never break on a rename.
    Returns the number of rows updated.
    """
    old_prefix = f"https://t.me/{old_username.lstrip('@')}/"
    new_prefix = f"https://t.me/{new_username.lstrip('@')}/"
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE series SET base_url = ? || substr(base_url, ?) WHERE base_url LIKE ?",
            (new_prefix, len(old_prefix) + 1, f"{old_prefix}%")
        )
        return cur.rowcount


def stop_series(series_id: int):
    with _connect() as conn:
        conn.execute("UPDATE series SET active = 0 WHERE id = ?", (series_id,))


def reactivate_series(series_id: int):
    """
    Re-enable tracking for a series stopped earlier (manually, by the "N з
    N" caption pattern, or by reaching the resolved total_episodes count).
    Used by the "🔄 Поновити відстеження" button after an auto-stop —
    started_at is left untouched, so the ~6-month tracking-age cutoff still
    counts from the original add date, not from the moment of renewal.
    """
    with _connect() as conn:
        conn.execute("UPDATE series SET active = 1 WHERE id = ?", (series_id,))


def set_total_episodes(series_id: int, total_episodes: int):
    """
    Persist the season's total episode count once DeepSeek resolves it
    (see analyzer.ai_cleaner.extract_total_episodes) — the checker compares
    against this on every new download to auto-stop tracking on the actual
    last episode.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE series SET total_episodes = ? WHERE id = ?",
            (total_episodes, series_id)
        )


def record_episode(series_id: int, season: int, episode: int):
    """
    Update last downloaded episode and insert episode record. Also resets
    total_episodes back to 0 when this episode belongs to a DIFFERENT
    season than the series' current last_season — one series row tracks
    every season of a title (last_season/last_episode just move forward),
    so a resolved total_episodes value is only valid for the season it was
    resolved for; carrying it over into a new season would compare the new
    season's (still small) episode count against the OLD season's total.
    """
    with _connect() as conn:
        row = conn.execute("SELECT last_season FROM series WHERE id = ?", (series_id,)).fetchone()
        if row and row["last_season"] != season:
            conn.execute("UPDATE series SET total_episodes = 0 WHERE id = ?", (series_id,))
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


def get_cached_caption(chat: str, message_id: int) -> tuple[int, int] | None:
    """
    Return the (season, episode) previously resolved for this exact Telegram
    message, if any. A message's caption never changes after posting, so once
    resolved via DeepSeek it never needs re-resolving on later check cycles —
    avoids burning an API call (and rate-limit budget) re-parsing the same
    16+ old episodes of a topic on every 6-hour check.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT season, episode FROM caption_cache WHERE chat = ? AND message_id = ?",
            (chat, message_id)
        ).fetchone()
    return (row["season"], row["episode"]) if row else None


def cache_caption(chat: str, message_id: int, season: int, episode: int):
    """Persist a resolved (season, episode) for a message so it's never re-parsed."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO caption_cache (chat, message_id, season, episode) "
            "VALUES (?, ?, ?, ?)",
            (chat, message_id, season, episode)
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
        logger.info(f"Deactivated {n} expired anime series.")


def _cutoff_date() -> str:
    return (datetime.now() - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
