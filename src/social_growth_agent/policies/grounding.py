"""Fabrication guards for discovery runs (Phase 9): links and numbers in a post must come
from the evidence the run was given.

``EvidenceGrounding`` is computed once, when a run is started from an opportunity, from
the stored evidence items (their canonical URLs and the numbers in their text), and is
kept in the run's ``ContentPolicy``. It is a deterministic floor, not a fact check: a
number that appears in the evidence can still be used wrongly, which is what the critic
and the human reviewer are for.
"""

import re

from pydantic import BaseModel, ConfigDict

from social_growth_agent.discovery.text import canonical_url, numbers_in

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_YEAR = re.compile(r"^(19|20)\d{2}$")
SMALL_NUMBER_LIMIT = 10
"""0-10 are exempt (list numbering, "3 ways", "two models")."""


class EvidenceGrounding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_urls: frozenset[str] = frozenset()
    allowed_numbers: frozenset[str] = frozenset()


def grounding_from(texts: list[str], urls: list[str | None]) -> EvidenceGrounding:
    allowed_urls = frozenset(u for raw in urls if (u := canonical_url(raw)) is not None)
    numbers: set[str] = set()
    for text in texts:
        numbers |= {_plain(n) for n in numbers_in(text)}
    return EvidenceGrounding(allowed_urls=allowed_urls, allowed_numbers=frozenset(numbers))


def unsupported_urls(content: str, grounding: EvidenceGrounding) -> list[str]:
    found = [m.group(0).rstrip(".,;:!?") for m in _URL.finditer(content)]
    return [u for u in found if canonical_url(u) not in grounding.allowed_urls]


def unsupported_numbers(content: str, grounding: EvidenceGrounding) -> list[str]:
    content_without_urls = _URL.sub(" ", content)
    out = []
    for raw in numbers_in(content_without_urls):
        plain = _plain(raw)
        bare = plain.rstrip("%")
        if _YEAR.match(bare):
            continue
        if bare.isdigit() and int(bare) <= SMALL_NUMBER_LIMIT and not plain.endswith("%"):
            continue
        if plain not in grounding.allowed_numbers:
            out.append(raw)
    return list(dict.fromkeys(out))


def _plain(number: str) -> str:
    return number.replace(",", "")
