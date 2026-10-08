"""What part of the conversation the model is sent: a bounded tail, never a broken one."""

from collections.abc import Sequence

from app.agent.llm.base import Message, TextBlock


def trim_history(messages: Sequence[Message], limit: int) -> list[Message]:
    """At most `limit` messages (the newest), but:

    * the latest user message is ALWAYS included, however long the turn has become;
    * the slice starts at a user message with text, because a `tool` message whose proposal was
      cut off (or an assistant message with no user message before it) is rejected by the API.
    """
    msgs = list(messages)
    if not msgs:
        return []
    last_user = max((i for i, m in enumerate(msgs) if m.role == "user" and _has_text(m)), default=0)
    start = min(max(0, len(msgs) - limit), last_user)
    while start < len(msgs) and not (msgs[start].role == "user" and _has_text(msgs[start])):
        start += 1
    return msgs[start:]


def _has_text(message: Message) -> bool:
    return any(isinstance(b, TextBlock) for b in message.content)
