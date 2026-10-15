"""Bounded, literal evidence extraction. HTML is data, never executable instructions."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser

from social_growth_agent.discovery.text import sanitize_text
from social_growth_agent.errors import InvalidDiscoveryRequestError

MAX_EXCERPTS = 12
MAX_EXCERPT_CHARS = 500
MAX_EVIDENCE_CHARS = 4000
EXTRACTOR_VERSION = "article-v2"
_TECH = re.compile(
    r"\b(model|agent|parameter|tool|local|device|hardware|memory|privacy|"
    r"inference|reasoning|latency|license|context|deployment|benchmark|limitation)\w*\b",
    re.IGNORECASE,
)


class ArticleParser(HTMLParser):
    """Collect prose blocks in semantic article/main containers, excluding UI and code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []
        self.blocks: list[tuple[bool, str]] = []
        self.current: list[str] = []
        self.block_tag: str | None = None
        self.article_block = False
        self.meta: dict[str, str] = {}
        self.complete = False
        self.started_article = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.meta[key.lower()] = values["content"] or ""
        if tag in {"meta", "link", "img", "br", "hr", "input", "source", "wbr"}:
            if tag == "br" and self.block_tag:
                self.current.append(" ")
            return
        hidden = any(s[1] for s in self.stack) or tag in {
            "script",
            "style",
            "nav",
            "footer",
            "header",
            "aside",
            "pre",
            "code",
            "form",
        }
        hidden = (
            hidden
            or "hidden" in values
            or "data-nosnippet" in values
            or values.get("aria-hidden") == "true"
        )
        article = any(s[2] for s in self.stack)
        if (
            not self.started_article
            and not hidden
            and (
                tag == "article"
                or any(
                    word in (values.get("class") or "").split()
                    for word in ("blog-content", "prose")
                )
            )
        ):
            article = True
            self.started_article = True
        self.stack.append((tag, hidden, article))
        if tag in {"p", "li", "h1", "h2", "h3"} and not hidden:
            self._finish()
            self.block_tag = tag
            self.article_block = article

    def handle_endtag(self, tag: str) -> None:
        if tag == self.block_tag:
            self._finish()
        if tag == "html":
            self.complete = True
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self.block_tag and not any(s[1] for s in self.stack):
            self.current.append(data)

    def _finish(self) -> None:
        if self.block_tag:
            value = sanitize_text("".join(self.current), limit=20_000)
            if value and self.block_tag in {"p", "li"}:
                self.blocks.append((self.article_block, value))
            elif value and self.article_block and self.block_tag == "h1":
                self.meta.setdefault("article:title", value)
        self.current = []
        self.block_tag = None


@dataclass(frozen=True)
class ArticleEvidence:
    title: str
    author: str | None
    published_at: datetime | None
    excerpts: tuple[str, ...]
    response_sha256: str
    extractor: str = EXTRACTOR_VERSION

    def evidence_text(self, publisher: str) -> str:
        attribution = self.author or publisher
        return (
            f"Source attribution: {attribution}. Self-reported source claims; "
            "not independently verified.\n" + "\n".join(self.excerpts)
        )


def extract_article(body: bytes, selected: list[str] | None = None) -> ArticleEvidence:
    parser = ArticleParser()
    parser.feed(body.decode("utf-8", errors="strict"))
    parser.close()
    if any(word in parser.meta.get("robots", "").lower() for word in ("noarchive", "nosnippet")):
        raise InvalidDiscoveryRequestError("source disallows evidence storage")
    if not parser.complete:
        raise InvalidDiscoveryRequestError("incomplete HTML document; no evidence stored")
    blocks = [value for article, value in parser.blocks if article]
    if not blocks:
        raise InvalidDiscoveryRequestError("no recognizable article prose; no evidence stored")
    sentences = list(
        dict.fromkeys(s.strip() for b in blocks for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", b))
    )
    if selected is not None:
        chosen = [sanitize_text(s, limit=MAX_EXCERPT_CHARS + 1) for s in selected]
        if any(s not in sentences and s not in blocks for s in chosen):
            raise InvalidDiscoveryRequestError("requested excerpt is not literal article evidence")
    else:
        # Round-robin categories keep deployment/tradeoffs from losing to benchmark prose.
        categories = (
            r"\bparameters?\b",
            r"\blarger\b|\blimitation\w*\b",
            r"\blocally\b|\blocal\b",
            r"\bsupport\b|\bbackend\b",
            r"\bmemory\b|\bhardware\b",
            r"\blicen[cs]e\b|\bopen.weight\b",
            r"\btools?\b",
            r"\bcontext\b",
            r"\breasoning\b",
            r"\bbenchmark\w*\b",
        )
        passages = [
            passage
            for block in blocks
            for passage in (
                [block]
                if len(block) <= MAX_EXCERPT_CHARS
                else re.split(r"(?<=[.!?])\s+(?=[A-Z])", block)
            )
        ]
        candidates = [s for s in passages if 40 <= len(s) <= MAX_EXCERPT_CHARS and _TECH.search(s)]
        chosen = []
        for pattern in categories:
            match = next(
                (s for s in candidates if s not in chosen and re.search(pattern, s, re.I)), None
            )
            if match:
                chosen.append(match)
        chosen.extend(s for s in candidates if s not in chosen)
    excerpts: list[str] = []
    budget = MAX_EVIDENCE_CHARS - 300
    for sentence in chosen:
        if len(sentence) > MAX_EXCERPT_CHARS:
            raise InvalidDiscoveryRequestError("excerpt exceeds the evidence limit")
        if len(excerpts) >= MAX_EXCERPTS or len(sentence) + 1 > budget:
            break
        if sentence not in excerpts:
            excerpts.append(sentence)
            budget -= len(sentence) + 1
    if len(excerpts) < 2 or sum(map(len, excerpts)) < 160:
        raise InvalidDiscoveryRequestError("insufficient substantive article evidence")
    date = parser.meta.get("article:published_time") or parser.meta.get("date")
    published = None
    if date:
        try:
            candidate = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if candidate.tzinfo is not None:
                published = candidate
        except ValueError:
            pass
    title = sanitize_text(
        parser.meta.get("og:title", parser.meta.get("article:title", blocks[0])), limit=200
    )
    author = sanitize_text(parser.meta.get("author", ""), limit=80) or None
    return ArticleEvidence(
        title, author, published, tuple(excerpts), hashlib.sha256(body).hexdigest()
    )
