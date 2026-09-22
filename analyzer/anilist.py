import logging

import httpx

logger = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"
ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english }
    episodes
    status
  }
}
"""


async def _search_once(client: httpx.AsyncClient, title: str) -> dict | None:
    try:
        resp = await client.post(
            ANILIST_URL,
            json={"query": ANILIST_QUERY, "variables": {"search": title}},
        )
        data = resp.json()
        return data.get("data", {}).get("Media")
    except Exception as e:
        logger.warning(f"[anilist] request failed for {title!r}: {e}")
        return None


async def get_episode_count(title: str) -> int | None:
    """
    Look up an anime's total episode count on AniList — a live,
    community-maintained anime database, free with no API key required —
    far more reliable than an LLM's static training knowledge, and often
    has the number even before a season finishes airing (unlike asking a
    model to recall it from memory).

    AniList's search is picky about the very long full titles typical of
    isekai/light-novel adaptations (e.g. "Hell Mode: Yarikomi-zuki no
    Gamer wa Haisettei no Isekai de Musou Suru" doesn't match, but "Hell
    Mode" alone does) — falls back to progressively shorter word-count
    prefixes of the title if the full title isn't found.

    Returns None if no match is found at all, OR if a match was found but
    AniList doesn't have an episode count yet (still airing, unannounced)
    — callers should treat both the same way (fall back to another
    source), the distinction is only visible in the log.
    """
    words = title.split()
    candidates = [title]
    for n in (8, 5, 3, 2):
        if len(words) > n:
            candidates.append(" ".join(words[:n]))

    async with httpx.AsyncClient(timeout=15) as client:
        for candidate in candidates:
            media = await _search_once(client, candidate)
            if media:
                episodes = media.get("episodes")
                logger.info(f"[anilist] {title!r} matched via {candidate!r} -> {media}")
                return episodes

    logger.info(f"[anilist] no match found for {title!r} (tried {len(candidates)} variants)")
    return None
