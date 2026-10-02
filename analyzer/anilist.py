import asyncio
import logging
import re

import httpx

logger = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"

_MEDIA_FIELDS = """
    id
    title { romaji english }
    episodes
    status
    format
    relations {
      edges {
        relationType
        node { id title { romaji english } format episodes status }
      }
    }
"""
ANILIST_SEARCH_QUERY = "query ($search: String) { Media(search: $search, type: ANIME) {" + _MEDIA_FIELDS + "} }"
ANILIST_BY_ID_QUERY = "query ($id: Int) { Media(id: $id, type: ANIME) {" + _MEDIA_FIELDS + "} }"

# Formats that count as "a season" when walking the SEQUEL chain — movies,
# OVAs and specials are sequels too, but they're not a season.
_SEASON_FORMATS = {"TV", "TV_SHORT", "ONA"}

# AniList keeps a split-cour season as separate entries ("... 2nd Season" and
# "... 2nd Season Part 2"), while the fansub/dub channels number it as ONE
# season. An entry like that is the second half of the previous one.
_CONTINUATION_RE = re.compile(r"\b(?:part|cour)\s*(?:2|3|ii|iii)\b|\b(?:2nd|second|3rd|third)\s+cour\b", re.IGNORECASE)

# How many SEQUEL edges to follow through non-season entries (an OVA or movie
# sitting between two seasons) before giving up on finding the next season.
_MAX_PASS_THROUGH_DEPTH = 4


async def _post(client: httpx.AsyncClient, query: str, variables: dict, label: str) -> dict | None:
    """
    One GraphQL request. AniList's anonymous API is rate limited and answers
    429 with a Retry-After, or `{"data": null, "errors": [...]}` — honour the
    wait (a couple of times) instead of treating it as "no such anime".
    """
    for attempt in range(3):
        try:
            resp = await client.post(ANILIST_URL, json={"query": query, "variables": variables})
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", "5") or 5), 30)
                logger.info(f"[anilist] rate limited on {label!r}, waiting {wait}s (attempt {attempt + 1}/3)")
                await asyncio.sleep(wait)
                continue
            return (resp.json().get("data") or {}).get("Media")
        except Exception as e:
            logger.warning(f"[anilist] request failed for {label!r}: {e}")
            return None
    return None


async def _fetch_by_id(client: httpx.AsyncClient, media_id: int, cache: dict) -> dict | None:
    """By-id lookup, memoised for the duration of one get_episode_count() call."""
    if media_id not in cache:
        cache[media_id] = await _post(client, ANILIST_BY_ID_QUERY, {"id": media_id}, f"id={media_id}")
    return cache[media_id]


async def _find_base_media(client: httpx.AsyncClient, title: str) -> dict | None:
    """
    AniList's search is picky about the very long full titles typical of
    isekai/light-novel adaptations (e.g. "Hell Mode: Yarikomi-zuki no Gamer
    wa Haisettei no Isekai de Musou Suru" doesn't match, but "Hell Mode"
    alone does) — falls back to progressively shorter word-count prefixes of
    the title if the full title isn't found.
    """
    words = title.split()
    candidates = [title]
    for n in (8, 5, 3, 2):
        if len(words) > n:
            candidates.append(" ".join(words[:n]))

    for candidate in candidates:
        media = await _post(client, ANILIST_SEARCH_QUERY, {"search": candidate}, candidate)
        if media:
            logger.info(f"[anilist] {title!r} matched via {candidate!r} -> {media['title']['romaji']!r}")
            return media
    logger.info(f"[anilist] no match found for {title!r} (tried {len(candidates)} variants)")
    return None


async def _next_entry(client: httpx.AsyncClient, media: dict, cache: dict) -> dict | None:
    """
    The nearest TV/ONA entry reachable from `media` through SEQUEL edges,
    searched breadth-first. Non-season entries (OVA, special, movie) are
    walked THROUGH, not stopped at: AniList often links a show to its next
    season only via an OVA that sits between them (Tensei shitara Slime
    Datta Ken's season 1 points at the "Coleus no Yume" OVA, and only that
    OVA points at season 2).
    """
    visited = {media["id"]}
    frontier = [media]
    for _ in range(_MAX_PASS_THROUGH_DEPTH):
        following = []
        for m in frontier:
            for edge in (m.get("relations") or {}).get("edges") or []:
                node = edge.get("node") or {}
                if edge.get("relationType") != "SEQUEL" or node.get("id") in visited:
                    continue
                visited.add(node["id"])
                full = await _fetch_by_id(client, node["id"], cache)
                if not full:
                    continue
                if node.get("format") in _SEASON_FORMATS:
                    return full
                following.append(full)
        frontier = following
        if not frontier:
            break
    return None


def _is_continuation(media: dict) -> bool:
    titles = media.get("title") or {}
    return any(_CONTINUATION_RE.search(titles.get(k) or "") for k in ("romaji", "english"))


async def _season_group(client: httpx.AsyncClient, media: dict, cache: dict) -> tuple[int | None, dict]:
    """
    A season as the channels count it: `media` plus any "Part 2"/"2nd cour"
    entries that continue it. Returns (summed episode count — None if any
    part's count isn't known yet, the last entry of the group).
    """
    total = media.get("episodes")
    last = media
    while True:
        nxt = await _next_entry(client, last, cache)
        if not nxt or not _is_continuation(nxt):
            return total, last
        total = total + nxt["episodes"] if total is not None and nxt.get("episodes") is not None else None
        last = nxt


async def get_episode_count(title: str, season: int = 1) -> int | None:
    """
    Look up the total episode count of a given SEASON on AniList — a live,
    community-maintained anime database, free with no API key required —
    far more reliable than an LLM's static training knowledge, and often
    has the number even before a season finishes airing.

    AniList keeps every season as a separate entry linked by SEQUEL
    relations, so the title search finds season 1 and season N is reached by
    stepping N-1 seasons along the chain (see _next_entry / _season_group for
    the OVA-in-between and split-cour wrinkles). Without this, season 2+ of
    a multi-season title would be given season 1's episode count.

    Returns None if the title or that season can't be found, OR if AniList
    has no episode count for it yet (still airing, unannounced) — callers
    treat both the same way (fall back to another source).
    """
    cache: dict = {}
    async with httpx.AsyncClient(timeout=15) as client:
        media = await _find_base_media(client, title)
        for step in range(1, season):
            if not media:
                break
            _total, last = await _season_group(client, media, cache)
            media = await _next_entry(client, last, cache)
            if not media:
                logger.info(f"[anilist] {title!r}: no season found after step {step} (wanted season {season})")
        if not media:
            return None
        total, _last = await _season_group(client, media, cache)
        logger.info(
            f"[anilist] {title!r} season {season} -> {media['title']['romaji']!r}: "
            f"episodes={total} status={media.get('status')}"
        )
        return total
