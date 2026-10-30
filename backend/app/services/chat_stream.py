"""流式输出（ChatService 的 StreamMixin — thin umbrella re-export）。

StreamMixin is split into 5 mixin modules following the chat_stream_output
sibling style. Each mixin owns one cohesive responsibility:

- `chat_stream_entry.StreamEntryMixin`: `processMessageStream` /
  `_processMessageStreamInner` + module-level `_OVERVIEW_TERMINAL_STATUS` /
  `_overviewStepPayload` (used by multi-step resume replay).
- `chat_stream_shortcuts.StreamShortcutMixin`: chitchat / clarify / intercept
  card branches (no SQL / no plan; lightweight event sequences).
- `chat_stream_query.StreamQueryMixin`: `_streamQuery` (single-step main path
  with feature short-circuit, retry fallback to multi-step).
- `chat_stream_multistep.StreamMultiStepMixin`: `_streamMultiStep` + 3 step
  helpers (`_stepResultEvent` / `_singleStepOverview` / `_singleStepStart`).
- `chat_stream_persist.StreamPersistMixin`: `persistInterruptedStream` (H4
  disconnect fallback).

Backward compatibility: `StreamMixin` is composed from the 5 mixins in the
same order they appear in `ChatService`'s MRO. Module-level helpers
(`_overviewStepPayload` / `_OVERVIEW_TERMINAL_STATUS`) are re-exported here
so any out-of-tree import keeps resolving.
"""

from app.services.chat_stream_entry import (
    StreamEntryMixin,
    _OVERVIEW_TERMINAL_STATUS,
    _overviewStepPayload,
)
from app.services.chat_stream_multistep import StreamMultiStepMixin
from app.services.chat_stream_persist import StreamPersistMixin
from app.services.chat_stream_query import StreamQueryMixin
from app.services.chat_stream_shortcuts import StreamShortcutMixin


class StreamMixin(
    StreamEntryMixin,
    StreamShortcutMixin,
    StreamQueryMixin,
    StreamMultiStepMixin,
    StreamPersistMixin,
):
    """流式输出（由 5 个职责 mixin 组合）。"""


__all__ = [
    "StreamMixin",
    "_OVERVIEW_TERMINAL_STATUS",
    "_overviewStepPayload",
]
