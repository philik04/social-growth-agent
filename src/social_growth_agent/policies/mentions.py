"""Mentions are manual-only (Phase 9).

X's automation rules forbid automated, unsolicited @mentions and replies. This module
decides from the *text alone* whether a post mentions an account, so no label, intent,
edit, schedule or worker can route a mention to automated publishing. The same function
is used at generation (a hard content rule), approval, the publish request, the worker's
pre-call check and the X publisher adapter itself.

A mention is ``@`` (or the full-width at sign U+FF20, which X also links) followed by 1-15 of
``[A-Za-z0-9_]``, not preceded by a letter, digit or underscore (so e-mail addresses are
not mentions). Detection deliberately errs towards "mention": a false positive costs a
manual post, a false negative could send an unsolicited mention.
"""

import re
import unicodedata

GROK_HANDLE = "grok"
MANUAL_ONLY_REASON = "contains an @mention: manual publication only (no automated mentions)"

_MENTION = re.compile(r"(?<![A-Za-z0-9_])[@\uFF20]([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])")


def find_mentions(text: str) -> list[str]:
    """Lowercased handles mentioned in ``text``, in order, duplicates kept."""
    normalized = unicodedata.normalize("NFKC", text)
    found = [m.lower() for m in _MENTION.findall(text)]
    if normalized != text:
        found += [m.lower() for m in _MENTION.findall(normalized) if m.lower() not in found]
    return found


def has_mention(text: str) -> bool:
    return bool(find_mentions(text))


def is_manual_only(text: str) -> bool:
    """True when this text may only ever be published by a person, by hand."""
    return has_mention(text)
