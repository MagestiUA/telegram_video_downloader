import os
import re
import logging
from config.config import settings

logger = logging.getLogger(__name__)

# Matches the "... - SxxExx.ext" suffix this project always generates
# (see generate_filename below), regardless of the title portion.
_EPISODE_FILE_RE = re.compile(r' - S(\d+)E(\d+)\.', re.IGNORECASE)


def sanitize_title(title: str, max_len: int = 120) -> str:
    """
    Sanitize a title for use as a filesystem folder/file name component.
    Clamped to max_len to stay well under typical filesystem NAME_MAX limits —
    multi-byte scripts (Cyrillic, etc.) use 2+ bytes per character in UTF-8,
    so a long localized title can silently blow past a 255-byte limit.
    """
    safe = "".join(c for c in title if c.isalnum() or c in " .()_-").strip()
    return safe[:max_len].rstrip()


def scan_existing_episodes(folder_path: str) -> set[tuple[int, int]]:
    """
    Scan a folder for files matching the "... - SxxExx.ext" naming convention
    and return the set of (season, episode) tuples already present on disk.

    Used so a title added to auto-tracking doesn't re-download episodes that
    were already fetched manually (e.g. via Normal/Batch mode) before tracking
    started — the folder is title-specific, so any SxxExx match inside it
    belongs to this title regardless of the exact title text in the filename.
    """
    found: set[tuple[int, int]] = set()
    if not os.path.isdir(folder_path):
        return found
    for entry in os.listdir(folder_path):
        m = _EPISODE_FILE_RE.search(entry)
        if m:
            found.add((int(m.group(1)), int(m.group(2))))
    return found


def generate_filename(canonical_name: str, season: int, episode: int, original_ext: str = ".mp4") -> str:
    """
    Generates the filename in format: 'Canonical Name - SxxExx.ext'
    """
    if season is None and episode is None:
        return f"{canonical_name}{original_ext}"
    if episode is None:
        # Season present but no episode? Treat as "S01" or just title
        # Let's assume just title + Season if available?
        # Actually for consistency let's just default to S01E01 if only season is missing, 
        # BUT if episode is missing it's likely a MOVIE or CLIP.
        return f"{canonical_name}{original_ext}"

    return f"{canonical_name} - S{season:02d}E{episode:02d}{original_ext}"

def get_target_path(canonical_name: str, filename: str) -> str:
    """
    Constructs the absolute path: Root_Dir/Canonical_Name/Filename
    """
    # Create folder if not exists
    folder_path = os.path.join(settings.DOWNLOAD_PATH, canonical_name)
    os.makedirs(folder_path, exist_ok=True)
    
    return os.path.join(folder_path, filename)
