"""Addressee resolution for multi-party group chat.

The deterministic layer answers a narrow question: *is this message aimed at
the bot, at another named participant, or at the floor?* It intentionally does
not call an LLM; the learned policy consumes these signals.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .models import MessageRecord

_AT_RE = re.compile(r"@([^\s@，,。.!！?？:：]{1,64})")
_REFERENCE_WORDS = ("回复", "引用", "reply", "quote")


@dataclass(frozen=True)
class AddresseeResult:
    """A compact, explainable addressee assessment."""

    score: float
    at_bot: bool
    name_mention: bool
    reply_to_bot: bool
    multi_at: bool
    other_at_names: tuple[str, ...]
    target_user_id: str
    target_name: str
    evidence: tuple[str, ...]


def _normalize(name: str) -> str:
    return str(name or "").strip().casefold()


def parse_at_names(text: str) -> tuple[str, ...]:
    """Extract ``@name`` tokens while dropping punctuation and duplicates."""

    names: list[str] = []
    for match in _AT_RE.finditer(text or ""):
        name = match.group(1).strip("，,。.!！?？:：")
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _matches(name: str, aliases: Iterable[str]) -> bool:
    normalized = _normalize(name)
    return any(normalized == _normalize(alias) or _normalize(alias) in normalized for alias in aliases if alias)


def resolve_addressee(
    current: MessageRecord,
    *,
    bot_names: Iterable[str],
    reply_target: MessageRecord | None = None,
    previous: MessageRecord | None = None,
) -> AddresseeResult:
    """Resolve who the current message is addressing.

    Args:
        current: Current user message.
        bot_names: Bot names / aliases, e.g. ``["柚子", "Yuzi"]``.
        reply_target: Message referenced by ``reply_to``, if available.
        previous: Previous message in the same session, for pronoun fallback.
    """

    aliases = tuple(str(name).strip() for name in bot_names if str(name).strip())
    at_names = parse_at_names(current.text)
    at_bot = any(_matches(name, aliases) for name in at_names)
    other_at_names = tuple(name for name in at_names if not _matches(name, aliases))
    lowered = current.text.casefold()
    name_mention = any(_normalize(alias) in lowered for alias in aliases if alias)
    reply_to_bot = bool(reply_target and reply_target.is_bot)
    if not reply_to_bot and any(word in current.text.casefold() for word in _REFERENCE_WORDS):
        reply_to_bot = any(f"回复@{alias}" in current.text or f"回复 {alias}" in current.text for alias in aliases)

    score = 0.0
    if at_bot:
        score += 0.55
    if name_mention:
        score += 0.25
    if reply_to_bot:
        score += 0.20
    multi_at = bool(at_bot and other_at_names)
    if multi_at:
        # Addressing the bot plus someone else is weaker than a one-to-one @.
        score *= 0.65
    if previous is not None and reply_to_bot and previous.is_bot:
        score += 0.05
    score = max(0.0, min(1.0, score))

    evidence: list[str] = []
    if at_bot:
        evidence.append("本条 @ 了机器人")
    if name_mention:
        evidence.append("本条叫了机器人名字")
    if reply_to_bot:
        evidence.append("本条引用了机器人的消息")
    if multi_at:
        evidence.append(f"本条同时 @ 了其他人：{', '.join(other_at_names)}")

    if reply_target is not None:
        target_user_id = reply_target.user_id
        target_name = reply_target.user_name
    else:
        target_user_id = current.user_id
        target_name = current.user_name
    return AddresseeResult(
        score=score,
        at_bot=at_bot,
        name_mention=name_mention,
        reply_to_bot=reply_to_bot,
        multi_at=multi_at,
        other_at_names=other_at_names,
        target_user_id=target_user_id,
        target_name=target_name,
        evidence=tuple(evidence),
    )
