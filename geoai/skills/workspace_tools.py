"""Workspace file-I/O tools."""

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

from ..context import current
from ..workspace import WorkspaceError

_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


def list_files(subdir: str = "", pattern: str = "*") -> list[str]:
    """Recursively list files under a subdir as sorted relative paths."""
    ctx = current()
    base = ctx.workspace.root if not subdir else ctx.workspace.resolve(subdir)
    return sorted(
        str(p.relative_to(ctx.workspace.root)).replace("\\", "/")
        for p in base.glob(f"**/{pattern}")
        if p.is_file()
    )


def find_files(pattern: str) -> list[str]:
    """Recursively find files matching a glob under the workspace root."""
    ctx = current()
    return sorted(
        str(p.relative_to(ctx.workspace.root)).replace("\\", "/")
        for p in ctx.workspace.root.rglob(pattern)
        if p.is_file()
    )


def read_file(path: str, max_bytes: int = 1_000_000) -> str:
    """Return file contents as UTF-8 text (errors replaced)."""
    ctx = current()
    resolved = ctx.workspace.resolve(path, must_exist=True)
    size = resolved.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file too large ({size} bytes > {max_bytes}): {path!r}")
    return resolved.read_text(encoding="utf-8", errors="replace")


def write_file(path: str, content: str) -> str:
    """Write UTF-8 text to a workspace file; returns the absolute path."""
    ctx = current()
    out = ctx.workspace.resolve(path, write=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    rel = out.relative_to(ctx.workspace.root).as_posix()
    ctx.workspace.record_output(rel)
    ctx.notify()
    return str(out)


def download(url: str, filename: str) -> str:
    """Stream a URL into ``data/`` (capped at 2 GB); returns the absolute path."""
    ctx = current()
    name = Path(filename).name
    out = ctx.workspace.resolve_under(ctx.workspace.data, name)
    out.parent.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(url, headers={"User-Agent": "geo-ai-harness"})
    with urllib.request.urlopen(req) as resp, open(out, "wb") as fh:
        total = 0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise ValueError(f"download exceeds the 2 GB cap: {url!r}")
            fh.write(chunk)

    rel = out.relative_to(ctx.workspace.root).as_posix()
    ctx.workspace.record_output(rel)
    ctx.notify()
    return str(out)


def import_data(source: str, dest_name: str | None = None) -> str:
    """Copy an external file or folder into the workspace ``data/`` folder.

    ``source`` is an absolute path outside the workspace; ``dest_name`` names the
    target under ``data/`` (defaults to the source basename). Name collisions get
    a numeric suffix. Returns the workspace-relative destination path.
    """
    ctx = current()
    src = Path(source).expanduser()
    if not src.exists():
        raise WorkspaceError(f"import source does not exist: {source!r}")

    target = Path(dest_name) if dest_name else Path(src.name)
    dest = ctx.workspace.data / target
    if dest.exists():
        stem, suffix = target.stem, target.suffix
        i = 1
        while dest.exists():
            dest = ctx.workspace.data / f"{stem}_{i}{suffix}"
            i += 1

    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)

    rel = dest.relative_to(ctx.workspace.root).as_posix()
    ctx.workspace.record_output(rel)
    ctx.notify()
    return rel


# Re-export Path for parity with the documented run_python namespace.
__all__ = ["list_files", "find_files", "read_file", "write_file", "download", "import_data"]
