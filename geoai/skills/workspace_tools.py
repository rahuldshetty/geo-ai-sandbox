"""Workspace file-I/O tools."""

from __future__ import annotations

import shutil
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from ..context import current
from ..workspace import WorkspaceError

_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
_PROGRESS_INTERVAL_BYTES = 4 * 1024 * 1024
_PROGRESS_INTERVAL_SECONDS = 0.25


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


def read_file(
    path: str,
    max_bytes: int = 1_000_000,
    offset: int = 0,
    limit: int | None = None,
) -> str:
    """Read a UTF-8 text file from the workspace (errors replaced).

    Reads the whole file by default, refusing anything larger than
    ``max_bytes``. For large files (e.g. Sentinel-1 annotation XML), read a
    byte range instead: ``offset`` is the 0-based byte to start at, ``limit``
    the max bytes to return (``None`` = to end of file). ``max_bytes`` still
    caps the returned slice.
    """
    ctx = current()
    resolved = ctx.workspace.resolve(path, must_exist=True)
    size = resolved.stat().st_size
    if limit is None:
        if size > max_bytes:
            raise ValueError(
                f"file too large ({size} bytes > {max_bytes}): {path!r}; "
                "read a slice with offset/limit"
            )
        return resolved.read_text(encoding="utf-8", errors="replace")
    with resolved.open("rb") as f:
        f.seek(offset)
        data = f.read(min(limit, max_bytes))
    return data.decode("utf-8", errors="replace")


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


def _filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    return name or "download"


def _unique_filenames(files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Avoid two parallel jobs writing the same workspace destination."""
    counts: dict[str, int] = {}
    result = []
    for url, filename in files:
        name = Path(filename).name or _filename_from_url(url)
        stem, suffix = Path(name).stem, Path(name).suffix
        count = counts.get(name, 0)
        counts[name] = count + 1
        if count:
            name = f"{stem}_{count}{suffix}"
        result.append((url, name))
    return result


def _emit_progress(ctx, event: dict) -> None:
    callback = ctx.download_progress
    if callable(callback):
        try:
            callback(event)
        except Exception:
            # Download persistence must not fail because a UI subscriber is
            # unavailable or has been disconnected.
            pass


def _download_one(ctx, url: str, filename: str, *, notify: bool = True) -> str:
    """Download one URL and emit lifecycle/progress events when configured."""
    job_id = uuid.uuid4().hex
    name = Path(filename).name or _filename_from_url(url)
    out = ctx.workspace.resolve_under(ctx.workspace.data, name)
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + f".{job_id}.part")
    base_event = {
        "id": job_id,
        "parent_cell_id": ctx.download_parent_id,
        "filename": name,
        "url": url,
        "status": "running",
        "bytes_downloaded": 0,
        "total_bytes": None,
        "path": None,
        "error": None,
    }
    _emit_progress(ctx, dict(base_event))

    req = urllib.request.Request(url, headers={"User-Agent": "geo-ai-harness"})
    downloaded = 0
    total_bytes = None
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(partial, "wb") as fh:
            raw_length = resp.headers.get("Content-Length")
            try:
                total_bytes = int(raw_length) if raw_length else None
            except (TypeError, ValueError):
                total_bytes = None
            if total_bytes is not None and total_bytes > _MAX_DOWNLOAD_BYTES:
                raise ValueError(f"download exceeds the 2 GB cap: {url!r}")

            last_reported = 0
            last_report_time = 0.0
            _emit_progress(
                ctx,
                {
                    **base_event,
                    "total_bytes": total_bytes,
                },
            )
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > _MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"download exceeds the 2 GB cap: {url!r}")
                fh.write(chunk)
                now = time.monotonic()
                if (
                    downloaded - last_reported >= _PROGRESS_INTERVAL_BYTES
                    or now - last_report_time >= _PROGRESS_INTERVAL_SECONDS
                ):
                    _emit_progress(
                        ctx,
                        {
                            **base_event,
                            "bytes_downloaded": downloaded,
                            "total_bytes": total_bytes,
                        },
                    )
                    last_reported = downloaded
                    last_report_time = now
        partial.replace(out)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        _emit_progress(
            ctx,
            {
                **base_event,
                "status": "error",
                "bytes_downloaded": downloaded,
                "total_bytes": total_bytes,
                "error": str(exc),
            },
        )
        raise

    rel = out.relative_to(ctx.workspace.root).as_posix()
    ctx.workspace.record_output(rel)
    _emit_progress(
        ctx,
        {
            **base_event,
            "status": "done",
            "bytes_downloaded": out.stat().st_size,
            "total_bytes": out.stat().st_size,
            "path": rel,
        },
    )
    if notify:
        ctx.notify()
    return rel


def download(url: str, filename: str) -> str:
    """Stream a URL into ``data/`` and return its workspace-relative path."""
    ctx = current()
    return _download_one(ctx, url, filename)


def download_files(files: list[dict[str, str]]) -> list[dict[str, str]]:
    """Download several URLs into ``data/`` concurrently.

    Each item must contain ``url`` and may contain ``filename``. Every file has
    its own progress event/cell, so a failed or completed download does not
    obscure the state of its siblings. The returned list preserves input order
    and contains one ``status``/``path`` or ``status``/``error`` result per job.
    """
    ctx = current()
    if not files:
        raise ValueError("files must contain at least one download")
    if len(files) > 20:
        raise ValueError("a maximum of 20 downloads can be started at once")
    requested: list[tuple[str, str]] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError("each download must provide a url")
        url = item["url"].strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("download URLs must use http:// or https://")
        requested.append((url, item.get("filename") or _filename_from_url(url)))
    requested = _unique_filenames(requested)

    results: list[dict[str, str] | None] = [None] * len(requested)
    with ThreadPoolExecutor(max_workers=min(6, len(requested))) as pool:
        futures = {
            pool.submit(_download_one, ctx, url, filename, notify=False): index
            for index, (url, filename) in enumerate(requested)
        }
        for future, index in futures.items():
            try:
                results[index] = {
                    "url": requested[index][0],
                    "filename": requested[index][1],
                    "status": "done",
                    "path": future.result(),
                }
            except Exception as exc:  # preserve sibling jobs while reporting failure
                results[index] = {
                    "url": requested[index][0],
                    "filename": requested[index][1],
                    "status": "error",
                    "error": str(exc),
                }
    ctx.notify()
    return [result for result in results if result is not None]


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
__all__ = [
    "list_files",
    "find_files",
    "read_file",
    "write_file",
    "download",
    "download_files",
    "import_data",
]
