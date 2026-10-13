"""Text handling for untrusted external content: sanitizing, canonical URLs, keywords.

Everything retrieved from a feed, X or Hacker News is untrusted data. It is reduced to
plain text here (no markup, no control or bidirectional-override characters, bounded
length) before it is stored or shown to any agent, and nothing in it is ever treated as an
instruction, a source to fetch or a recipient.
"""

import html
import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_TEXT_CHARS = 500
MAX_TITLE_CHARS = 200

# Bidirectional overrides/isolates and zero-width characters can hide or reorder text.
_INVISIBLE = {
    *range(0x200B, 0x2010),
    *range(0x202A, 0x202F),
    *range(0x2060, 0x2070),
    0xFEFF,
}
_TAG = re.compile(r"<[^>]{0,2000}>")
_SPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_WORD = re.compile(r"[a-z0-9][a-z0-9.+#-]*[a-z0-9+#]|[a-z0-9]")
_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|s|t)$", re.IGNORECASE)

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "like",
        "may",
        "might",
        "more",
        "most",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "about",
        "after",
        "all",
        "also",
        "any",
        "because",
        "before",
        "being",
        "both",
        "each",
        "few",
        "get",
        "got",
        "here",
        "him",
        "his",
        "her",
        "its",
        "itself",
        "let",
        "me",
        "much",
        "new",
        "now",
        "off",
        "once",
        "only",
        "other",
        "own",
        "same",
        "she",
        "should",
        "some",
        "such",
        "through",
        "under",
        "until",
        "very",
        "via",
        "want",
        "way",
        "well",
        "yet",
        "one",
        "two",
        "use",
        "used",
        "using",
        "make",
        "made",
        "today",
    ]
)


def sanitize_text(raw: str | None, *, limit: int = MAX_TEXT_CHARS) -> str:
    """Plain, bounded text: HTML tags removed, entities decoded, control and invisible
    characters dropped, whitespace collapsed. Never raises."""
    if not raw:
        return ""
    text = _TAG.sub(" ", raw)
    text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    kept = []
    for ch in text:
        code = ord(ch)
        if code in _INVISIBLE:
            continue
        if ch in "\n\t":
            kept.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co"):
            continue
        else:
            kept.append(ch)
    text = _SPACE.sub(" ", "".join(kept))
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def canonical_url(raw: str | None) -> str | None:
    """https URL without fragment, tracking parameters, default port or trailing slash.
    Anything that is not an http(s) URL with a host returns None (never fetched)."""
    if not raw:
        return None
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not _TRACKING.match(k)
    ]
    path = parts.path.rstrip("/") or ""
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if host is None:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def tokens(text: str) -> list[str]:
    """Lowercase content words (stopwords and pure numbers removed)."""
    found = _WORD.findall(text.lower())
    return [t for t in found if t not in STOPWORDS and not t.isdigit() and len(t) > 1]


def token_set(text: str) -> frozenset[str]:
    return frozenset(tokens(text))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def matched_terms(text: str, vocabulary: Sequence[str]) -> list[str]:
    """Vocabulary terms (single words or phrases) that occur in the text, in vocabulary
    order. Matching is on word boundaries and case-insensitive."""
    lowered = f" {' '.join(_WORD.findall(text.lower()))} "
    hits = []
    for term in vocabulary:
        needle = " ".join(_WORD.findall(term.lower()))
        if needle and f" {needle} " in lowered:
            hits.append(term.lower())
    return list(dict.fromkeys(hits))


_NUMBER = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)*%?|\d+(?:\.\d+)+)(?![\w])")
_VERSION = re.compile(r"(?<![\w])v?\d+(?:\.\d+){1,3}(?:[-.]?[a-z]+\d*)?(?![\w])", re.IGNORECASE)
_YEAR = re.compile(r"^(19|20)\d{2}$")


def numbers_in(text: str) -> list[str]:
    """Numeric tokens as written (``76x`` → ``76``, ``96%`` → ``96%``, ``3.5`` → ``3.5``)."""
    return [m.group(1) for m in _NUMBER.finditer(text)]


def anchors_in(text: str) -> set[str]:
    """Specific, checkable tokens: version strings and numbers that are not bare years or
    single digits. Used to decide whether two sources state the *same* specific fact."""
    found: set[str] = set()
    for match in _VERSION.finditer(text):
        found.add(match.group(0).lower().lstrip("v"))
    for number in numbers_in(text):
        plain = number.rstrip("%").replace(",", "")
        if _YEAR.match(plain) or (plain.isdigit() and len(plain) < 2):
            continue
        found.add(number.replace(",", ""))
    return found
