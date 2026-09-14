"""What a tool is allowed to touch.

The session layer derives replay-safety from these flags instead of keeping a
hand-maintained allowlist of tool names: a run may only be replayed after a
provider failure when no tool that mutated the workspace or the live map has
completed yet.
"""

from __future__ import annotations

from enum import StrEnum


class Effect(StrEnum):
    """One observable side effect a tool may have."""

    READ = "read"
    WORKSPACE_WRITE = "workspace_write"
    MAP_WRITE = "map_write"
    NETWORK = "network"
    PROCESS = "process"


#: Effects that make a run unsafe to replay from the start.
MUTATING_EFFECTS: frozenset[Effect] = frozenset(
    {Effect.WORKSPACE_WRITE, Effect.MAP_WRITE}
)

#: Effects a tool may only have when the user granted approval.
APPROVAL_EFFECTS: frozenset[Effect] = frozenset({Effect.PROCESS})

READ_ONLY: frozenset[Effect] = frozenset({Effect.READ})
