"""Dispatch requirement completeness ASK items across available HITL channels."""

from __future__ import annotations

from pathlib import Path
import string
import urllib.error
import urllib.parse
import urllib.request

from codd.hitl_session import HitlSession, is_claude_code_env
from codd.lexicon import AskItem


DEFAULT_CHANNELS = ["askuserquestion", "ntfy", "lexicon"]
SEVERITY_ORDER = ["critical", "high", "medium", "low"]


def send_ask_items(
    ask_items: list[AskItem],
    channels: list[str] = DEFAULT_CHANNELS,
    ntfy_topic: str = "",
    lexicon_path: str | Path | None = None,
    ntfy_severity_threshold: str = "critical",
) -> None:
    """Send ASK items through Claude, ntfy, and lexicon channels when available."""
    normalized_channels = {channel.lower() for channel in channels}

    if "askuserquestion" in normalized_channels and is_claude_code_env():
        for item in ask_items:
            _send_ask_user_question(item)

    if "ntfy" in normalized_channels and ntfy_topic:
        for item in ask_items:
            if not _severity_at_or_above(_ask_item_severity(item), ntfy_severity_threshold):
                continue
            _post_ntfy(ntfy_topic, format_ask_for_ntfy(item))

    if "lexicon" in normalized_channels and lexicon_path is not None:
        HitlSession(list(ask_items)).save_to_lexicon(Path(lexicon_path))


def format_ask_for_ntfy(item: AskItem) -> str:
    """Format an ASK item as a compact ntfy message."""
    options = []
    for index, option in enumerate(item.options):
        marker = string.ascii_uppercase[index] if index < len(string.ascii_uppercase) else str(index + 1)
        suffix = " (recommended)" if option.recommended else ""
        options.append(f"[{marker}] {option.label}{suffix}")
    if not options:
        options.append("[free text]")
    return f"Q: {item.question} {' / '.join(options)}"


def parse_user_answer(raw: str, item: AskItem) -> str:
    """Convert a letter answer to an option id, otherwise preserve free text."""
    answer = raw.strip()
    if not answer:
        return ""

    for index, option in enumerate(item.options):
        marker = string.ascii_uppercase[index]
        if answer.upper() == marker:
            return option.id
        if answer == option.id:
            return option.id
        if answer.lower() == option.label.lower():
            return option.id
    return answer


def _severity_at_or_above(item_severity: str, threshold: str) -> bool:
    """Return True when item_severity is at least as severe as threshold."""
    try:
        return SEVERITY_ORDER.index(item_severity) <= SEVERITY_ORDER.index(threshold)
    except ValueError:
        return True


def _ask_item_severity(item: AskItem) -> str:
    severity = getattr(item, "severity", "")
    if isinstance(severity, str) and severity:
        return severity.lower()
    return "critical" if item.blocking else "high"


def _send_ask_user_question(item: AskItem) -> bool:
    """Best-effort hook for Claude Code AskUserQuestion integrations."""
    try:
        from claude_code import AskUserQuestion  # type: ignore[import-not-found]
    except Exception:
        return False

    try:
        AskUserQuestion(  # type: ignore[operator]
            question=item.question,
            options=[
                {
                    "id": option.id,
                    "label": option.label,
                    "description": option.description,
                    "recommended": option.recommended,
                }
                for option in item.options
            ],
        )
    except Exception:
        return False
    return True


def _post_ntfy(topic: str, message: str) -> bool:
    url = _ntfy_url(topic)
    request = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5):
            return True
    except (OSError, urllib.error.URLError):
        return False


def _ntfy_url(topic: str) -> str:
    if topic.startswith("http://") or topic.startswith("https://"):
        return topic
    return f"https://ntfy.sh/{urllib.parse.quote(topic.strip())}"
