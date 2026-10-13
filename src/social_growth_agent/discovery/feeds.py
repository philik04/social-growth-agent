"""RSS 2.0 and Atom parsing with ``defusedxml``: no DTDs, no entities, no external
references. Returns plain, sanitized entries; raw XML never leaves this module."""

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from social_growth_agent.discovery.text import (
    MAX_TITLE_CHARS,
    canonical_url,
    sanitize_text,
)

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
MAX_FEED_BYTES = 2_000_000


class FeedParseError(ValueError):
    """The document is not a feed this module accepts (the message is safe to show)."""


@dataclass(frozen=True)
class FeedEntry:
    entry_id: str
    url: str | None
    title: str
    summary: str
    author: str | None
    published_at: datetime | None


def parse_feed(body: bytes, *, max_items: int) -> list[FeedEntry]:
    """Newest-first is not assumed: entries are returned in document order, at most
    ``max_items``. Raises ``FeedParseError`` for oversize, malformed or unsafe XML."""
    if len(body) > MAX_FEED_BYTES:
        raise FeedParseError(f"feed larger than {MAX_FEED_BYTES} bytes")
    try:
        root = fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise FeedParseError(f"unsafe XML refused ({type(exc).__name__})") from None
    except Exception as exc:  # ParseError and friends: never echo the document
        raise FeedParseError(f"malformed XML ({type(exc).__name__})") from None
    if root.tag == f"{ATOM}feed":
        entries = [_atom_entry(e) for e in root.findall(f"{ATOM}entry")[:max_items]]
    elif root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise FeedParseError("RSS document without a channel")
        entries = [_rss_item(e) for e in channel.findall("item")[:max_items]]
    else:
        raise FeedParseError("not an RSS 2.0 or Atom document")
    return [e for e in entries if e is not None]


def _text(element: Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext())


def _rss_item(item: Element) -> FeedEntry | None:
    title = sanitize_text(_text(item.find("title")), limit=MAX_TITLE_CHARS)
    link = canonical_url(_text(item.find("link")).strip())
    guid = _text(item.find("guid")).strip()
    entry_id = guid or link or title
    if not entry_id or not title:
        return None
    author = _text(item.find(f"{DC}creator")) or _text(item.find("author"))
    return FeedEntry(
        entry_id=entry_id[:512],
        url=link,
        title=title,
        summary=sanitize_text(_text(item.find("description"))),
        author=sanitize_text(author, limit=80) or None,
        published_at=_rss_date(_text(item.find("pubDate")) or _text(item.find(f"{DC}date"))),
    )


def _atom_entry(entry: Element) -> FeedEntry | None:
    title = sanitize_text(_text(entry.find(f"{ATOM}title")), limit=MAX_TITLE_CHARS)
    link = None
    for candidate in entry.findall(f"{ATOM}link"):
        if candidate.get("rel", "alternate") == "alternate":
            link = canonical_url(candidate.get("href"))
            break
    entry_id = _text(entry.find(f"{ATOM}id")).strip() or link or title
    if not entry_id or not title:
        return None
    summary = _text(entry.find(f"{ATOM}summary")) or _text(entry.find(f"{ATOM}content"))
    author = entry.find(f"{ATOM}author")
    name = _text(author.find(f"{ATOM}name")) if author is not None else ""
    published = _text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))
    return FeedEntry(
        entry_id=entry_id[:512],
        url=link,
        title=title,
        summary=sanitize_text(summary),
        author=sanitize_text(name, limit=80) or None,
        published_at=_iso_date(published),
    )


def _rss_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return _iso_date(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def describe_empty(body: bytes, content_type: str | None) -> str:
    """Why a feed that parsed gave no entries, from the document's own structure: item
    counts and the channel's build date. Never quotes item text."""
    root = fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    if root.tag == f"{ATOM}feed":
        raw = len(root.findall(f"{ATOM}entry"))
        built = _text(root.find(f"{ATOM}updated")).strip()
        kind = "Atom feed"
    else:
        channel = root.find("channel")
        raw = 0 if channel is None else len(channel.findall("item"))
        built = (
            ""
            if channel is None
            else (_text(channel.find("lastBuildDate")) or _text(channel.find("pubDate"))).strip()
        )
        kind = "RSS channel"
    parts = [f"{kind} with {raw} item(s)"]
    if raw:
        parts.append("none had both an id and a title")
    if built:
        parts.append(f"built {sanitize_text(built, limit=40)}")
    parts.append(f"{len(body)} bytes")
    if content_type:
        parts.append(sanitize_text(content_type, limit=60))
    return "; ".join(parts)
