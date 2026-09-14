"""Workspace path confinement, in one place.

Every tool that turns a model-supplied string into a filesystem path goes
through :func:`resolve_workspace_path`, so confinement has a single
implementation instead of one inline ``resolve(write=True)`` per service.
"""

from __future__ import annotations

from pathlib import Path

from ..contracts.errors import WorkspaceError

#: Fixed workspace layout.
DATA = "data"
RESULTS = "results"
MAPS = "maps"
TRACES = "traces"

#: Directories a tool may write into, in layout order.
WRITABLE_DIRS: tuple[str, ...] = (RESULTS, MAPS, DATA)

#: Directories a tool may read from.
READABLE_DIRS: tuple[str, ...] = (DATA, RESULTS, MAPS, TRACES)


def is_within(path: Path, base: Path) -> bool:
    """Return whether ``path`` is ``base`` or lives underneath it."""
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def resolve_within(base: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``base``, rejecting absolute paths and escapes."""
    candidate = Path(rel)
    if candidate.is_absolute():
        raise WorkspaceError(f"absolute paths are not allowed: {rel!r}")
    resolved = (base / candidate).resolve()
    if not is_within(resolved, base.resolve()):
        raise WorkspaceError(f"path escapes {base.name}/: {rel!r}")
    return resolved


def resolve_workspace_path(
    root: Path,
    rel: str,
    *,
    must_exist: bool = False,
    write: bool = False,
) -> Path:
    """Resolve a workspace-relative path and enforce confinement.

    Args:
        root: The workspace root.
        rel: A workspace-relative path (absolute paths are rejected).
        must_exist: Require the resolved path to exist.
        write: Require the target to sit under ``results/``, ``maps/``, or
            ``data/``.

    Raises:
        WorkspaceError: On escape, missing file, or a forbidden write target.
    """
    resolved = resolve_within(root, rel)
    if write:
        allowed = tuple((root / name).resolve() for name in WRITABLE_DIRS)
        if not any(is_within(resolved, base) for base in allowed):
            raise WorkspaceError(
                "write target must be under results/, maps/, or data/: " f"{rel!r}"
            )
    if must_exist and not resolved.exists():
        raise WorkspaceError(f"file does not exist: {rel!r}")
    return resolved
