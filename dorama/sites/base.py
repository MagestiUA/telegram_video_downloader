from abc import ABC, abstractmethod


class BaseSiteHandler(ABC):
    """
    Abstract base class for video site handlers.
    To add a new site — create a subclass, set DOMAINS, implement all methods.
    """
    DOMAINS: list[str] = []

    @abstractmethod
    def is_valid_url(self, url: str) -> bool:
        """Quick syntactic check that this URL is a series this handler can track."""

    @abstractmethod
    async def get_series_title(self, url: str) -> str | None:
        """Fetch a clean series title from the given URL."""

    @abstractmethod
    async def list_episodes(self, url: str) -> list[dict]:
        """
        Return ALL currently available DUB (Ukrainian voice-over) episodes.
        Subtitle-only tracks and unsupported players are skipped.

        Each item: {"season": int, "episode": int, "source": str}
        where `source` is either a direct .m3u8 URL or a player page URL
        that download() knows how to resolve.
        """

    @abstractmethod
    async def download(self, source: str, title: str, season: int, episode: int,
                       path: str, notify_msg=None) -> bool:
        """
        Download one episode to <path>/<title>/<title> - S01E01.ext
        `source` — direct m3u8 or player page URL (from list_episodes).
        notify_msg — optional Pyrogram Message to update with progress.
        Returns True on success.
        """
