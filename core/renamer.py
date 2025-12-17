import os
import logging
from config.config import settings

logger = logging.getLogger(__name__)

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
