import asyncio
import json
import logging
import os
import re
import time

import httpx

from dorama.sites.base import BaseSiteHandler

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Voice-over titles that indicate SUBTITLES (not dub) — these tracks are skipped.
_SUBTITLE_MARKERS = ("субтитр", "sub", "оригінал")

# Safety caps for per-episode probing.
_MAX_SEASONS = 30
_MAX_EPISODES = 300


class UafixHandler(BaseSiteHandler):
    DOMAINS = ["uafix.net"]

    # ------------------------------------------------------------------ http

    async def _fetch(self, url: str, referer: str = "https://uafix.net/") -> str | None:
        headers = {"User-Agent": _UA, "Referer": referer}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as c:
                r = await c.get(url)
                if r.status_code == 200:
                    return r.text
                logger.debug(f"HTTP {r.status_code} for {url}")
        except Exception as e:
            logger.error(f"fetch({url}): {e}")
        return None

    # ------------------------------------------------------------------ validation / title

    def is_valid_url(self, url: str) -> bool:
        return "uafix.net/" in url

    async def get_series_title(self, url: str) -> str | None:
        html = await self._fetch(url)
        if not html:
            return None

        m = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
            html, re.IGNORECASE
        )
        if not m:
            m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL | re.IGNORECASE)
        if not m:
            m = re.search(r'<title>(.*?)</title>', html, re.DOTALL)
        if not m:
            return None

        raw = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        raw = re.sub(r'^Серіал\s+', '', raw, flags=re.IGNORECASE)   # drop "Серіал " prefix
        raw = re.sub(r'\s*/\s*.*', '', raw)                          # drop " / alt titles..."
        # cut at season / episode / online keywords
        raw = re.split(r'\s+\d+\s+(?:сезон|серія)', raw, flags=re.IGNORECASE)[0]
        raw = re.split(r'\s+(?:дивит|онлайн|дорама)|\s*\(', raw, flags=re.IGNORECASE)[0]
        return raw.strip() or None

    # ------------------------------------------------------------------ list episodes

    async def list_episodes(self, url: str) -> list[dict]:
        """Dispatch on URL format: per-episode page vs whole-serial player."""
        if re.search(r'season-\d+-episode-\d+', url):
            return await self._list_per_episode(url)
        return await self._list_serial(url)

    # ---- format A: per-episode pages (season-XX-episode-YY) ----

    def _parse_episode_url(self, url: str) -> tuple[str, int]:
        m = re.match(
            r'(https://(?:www\.)?uafix\.net/[^/]+/[^/]+/)season-(\d+)-episode-\d+/',
            url
        )
        if not m:
            raise ValueError(f"Cannot parse uafix episode URL: {url}")
        return m.group(1), int(m.group(2))

    async def _find_dub_player(self, base_url: str, season: int, episode: int) -> str | None:
        """Return zetvideo.net player URL (dub) for an episode page, or None."""
        ep_url = f"{base_url}season-{season:02d}-episode-{episode:02d}/"
        html = await self._fetch(ep_url)
        if not html:
            return None
        m = re.search(r'src=["\'](https://zetvideo\.net/vod/\d+)["\']', html)
        return m.group(1) if m else None

    async def _list_per_episode(self, first_ep_url: str) -> list[dict]:
        base_url, start_season = self._parse_episode_url(first_ep_url)
        episodes: list[dict] = []

        season = start_season
        while season < start_season + _MAX_SEASONS:
            found_in_season = False
            episode = 1
            while episode <= _MAX_EPISODES:
                player = await self._find_dub_player(base_url, season, episode)
                if not player:
                    break
                episodes.append({"season": season, "episode": episode, "source": player})
                found_in_season = True
                episode += 1
            if not found_in_season:
                break  # this season's E01 has no dub yet → stop
            season += 1

        return episodes

    # ---- format B: whole-serial player (zetvideo.net/serial/{id}) ----

    async def _list_serial(self, uafix_url: str) -> list[dict]:
        html = await self._fetch(uafix_url)
        if not html:
            return []

        m = re.search(r'src=["\'](https://zetvideo\.net/serial/\d+)["\']', html)
        if not m:
            logger.info(f"No zetvideo serial player on {uafix_url}")
            return []

        serial_html = await self._fetch(m.group(1), referer="https://uafix.net/")
        if not serial_html:
            return []

        jm = re.search(r"file:\s*'(\[.*?\}\])'\s*,", serial_html, re.DOTALL)
        if not jm:
            logger.error("Cannot find file JSON in zetvideo serial page")
            return []

        try:
            data = json.loads(jm.group(1))
        except Exception as e:
            logger.error(f"JSON parse error in serial player: {e}")
            return []

        # Pick first voice-over track that is NOT subtitles.
        dub = None
        for vo in data:
            title = (vo.get("title") or "").lower()
            if any(marker in title for marker in _SUBTITLE_MARKERS):
                continue
            dub = vo
            break
        if not dub:
            logger.info(f"{uafix_url}: only subtitle tracks — waiting for dub")
            return []

        episodes: list[dict] = []
        for season_folder in dub.get("folder", []):
            sm = re.search(r'\d+', season_folder.get("title", ""))
            season_num = int(sm.group()) if sm else 1
            for ep in season_folder.get("folder", []):
                em = re.search(r'\d+', ep.get("title", ""))
                file_url = ep.get("file")
                if not em or not file_url:
                    continue
                episodes.append({
                    "season": season_num,
                    "episode": int(em.group()),
                    "source": file_url,
                })
        return episodes

    # ------------------------------------------------------------------ m3u8 resolution

    async def _get_m3u8(self, source: str) -> str | None:
        """Resolve a `source` (direct m3u8 or player page) into a playable m3u8 URL."""
        if ".m3u8" in source.split("?")[0]:
            return source  # already a direct playlist (serial JSON)
        if "zetvideo.net" in source:
            return await self._m3u8_zetvideo(source)
        if "ashdi.vip" in source:
            return await self._m3u8_ashdi(source)
        return await self._m3u8_generic(source)

    async def _m3u8_zetvideo(self, player_url: str) -> str | None:
        html = await self._fetch(player_url, referer="https://uafix.net/")
        if not html:
            return None
        m = re.search(r'file:\s*["\'](https://zetvideo\.net/[^"\']+\.m3u8[^"\']*)["\']', html)
        return m.group(1) if m else None

    async def _m3u8_ashdi(self, player_url: str) -> str | None:
        html = await self._fetch(player_url)
        if not html:
            return None
        m = re.search(r"file:\s*['\"](https://ashdi\.vip/video[^'\"]+\.m3u8[^'\"]*)", html)
        return m.group(1).strip() if m else None

    async def _m3u8_generic(self, player_url: str) -> str | None:
        html = await self._fetch(player_url)
        if not html:
            return None
        m = re.search(r'file:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', html)
        return m.group(1) if m else None

    # ------------------------------------------------------------------ download

    async def download(self, source: str, title: str, season: int, episode: int,
                       path: str, notify_msg=None) -> bool:
        m3u8 = await self._get_m3u8(source)
        if not m3u8:
            logger.error(f"No m3u8 resolved for {source}")
            return False

        safe = "".join(c for c in title if c.isalnum() or c in " .()_-").strip()
        out_dir = os.path.join(path, safe)
        os.makedirs(out_dir, exist_ok=True)
        output = os.path.join(out_dir, f"{safe} - S{season:02d}E{episode:02d}.%(ext)s")

        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-playlist",
            "--newline",
            "--progress",
            "-f", "bestvideo+bestaudio/best",
            "-o", output,
            m3u8,
        ]
        logger.info(f"yt-dlp: {safe} S{season:02d}E{episode:02d} | {m3u8[:80]}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )

            last_edit = 0.0
            stderr_lines: list[str] = []

            async def _read_stderr():
                nonlocal last_edit
                async for raw in proc.stderr:
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    stderr_lines.append(line)
                    logger.debug(f"yt-dlp: {line}")

                    if notify_msg and "[download]" in line and "%" in line:
                        pm = re.search(
                            r"(\d+\.?\d*)%\s+of\s+~?\s*([\d.]+\s*\S+)\s+at\s+([\d.]+\s*\S+/s)\s+ETA\s+(\S+)",
                            line,
                        )
                        now = time.monotonic()
                        if pm and now - last_edit >= 5:
                            pct, size, speed, eta = pm.group(1), pm.group(2), pm.group(3), pm.group(4)
                            try:
                                await notify_msg.edit_text(
                                    f"🎬 **{title}** S{season:02d}E{episode:02d}\n"
                                    f"⏬ {pct}% з {size}\n"
                                    f"🚀 {speed} | ETA {eta}"
                                )
                                last_edit = now
                            except Exception:
                                pass

            await asyncio.gather(_read_stderr(), proc.wait())

            if proc.returncode == 0:
                logger.info(f"✅ Done: {safe} S{season:02d}E{episode:02d}")
                return True

            tail = "\n".join(stderr_lines[-5:])
            logger.error(f"yt-dlp exit {proc.returncode}:\n{tail}")

        except Exception as e:
            logger.error(f"yt-dlp exec error: {e}")

        return False
