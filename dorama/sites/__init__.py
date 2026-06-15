from urllib.parse import urlparse

from dorama.sites.base import BaseSiteHandler
from dorama.sites.uafix import UafixHandler

# Registry: domain -> handler class
# To add a new site: create a handler in sites/, add it here.
_HANDLERS: dict[str, type[BaseSiteHandler]] = {}

for _cls in [UafixHandler]:
    for _domain in _cls.DOMAINS:
        _HANDLERS[_domain] = _cls


def get_handler(url: str) -> BaseSiteHandler | None:
    """Return the appropriate site handler for a given URL, or None if unsupported."""
    domain = urlparse(url).netloc.removeprefix("www.")
    cls = _HANDLERS.get(domain)
    return cls() if cls else None


def supported_domains() -> list[str]:
    return list(_HANDLERS.keys())
