"""Workspace: a per-notebook results tree with path confinement."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class WorkspaceError(ValueError):
    """Raised when a path violates workspace confinement or a file contract."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Workspace:
    """A workspace rooted at ``<repo>/workspaces/<name>/``.

    Layout: ``data/`` (inputs), ``results/`` (outputs), ``maps/`` (saved
    ``.geolibre.json`` projects), plus a ``workspace.json`` manifest recording
    outputs and a monotonically increasing ``version`` counter.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._manifest_path = self.root / "workspace.json"

    # -- layout ----------------------------------------------------------

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def maps(self) -> Path:
        return self.root / "maps"

    # -- lifecycle -------------------------------------------------------

    def create(self) -> "Workspace":
        """Create the directory tree and manifest; idempotent."""
        for d in (self.data, self.results, self.maps):
            d.mkdir(parents=True, exist_ok=True)
        if not self._manifest_path.exists():
            self._write_manifest(
                {"name": self.root.name, "created_at": _now_iso(), "outputs": [], "version": 0}
            )
        return self

    # -- path confinement ------------------------------------------------

    def resolve(self, rel: str, *, must_exist: bool = False, write: bool = False) -> Path:
        """Resolve a workspace-relative path and enforce confinement.

        Rejects absolute paths and anything escaping the root (``..``).
        ``write=True`` additionally requires the target to sit under ``results/``,
        ``maps/``, or ``data/``. ``must_exist=True`` requires the file to exist.
        """
        p = Path(rel)
        if p.is_absolute():
            raise WorkspaceError(f"absolute paths are not allowed: {rel!r}")
        candidate = (self.root / p).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            raise WorkspaceError(f"path escapes the workspace root: {rel!r}")

        if write:
            allowed = (self.results.resolve(), self.maps.resolve(), self.data.resolve())
            if not any(_is_within(candidate, base) for base in allowed):
                raise WorkspaceError(
                    f"write target must be under results/, maps/, or data/: {rel!r}"
                )

        if must_exist and not candidate.exists():
            raise WorkspaceError(f"file does not exist: {rel!r}")

        return candidate

    def resolve_under(self, base: Path, rel: str) -> Path:
        """Resolve ``rel`` confined to ``base`` (for tools that pin a subdir)."""
        p = Path(rel)
        if p.is_absolute():
            raise WorkspaceError(f"absolute paths are not allowed: {rel!r}")
        candidate = (base / p).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            raise WorkspaceError(f"path escapes {base.name}/: {rel!r}")
        return candidate

    # -- manifest --------------------------------------------------------

    def _read_manifest(self) -> dict:
        if self._manifest_path.exists():
            try:
                return json.loads(self._manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"name": self.root.name, "created_at": _now_iso(), "outputs": [], "version": 0}

    def _write_manifest(self, manifest: dict) -> None:
        manifest.setdefault("created_at", _now_iso())
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def bump(self) -> None:
        """Increment the manifest ``version`` counter."""
        manifest = self._read_manifest()
        manifest["version"] = int(manifest.get("version", 0)) + 1
        self._write_manifest(manifest)

    def record_output(self, rel: str) -> None:
        """Append ``rel`` to the manifest outputs and bump the version."""
        manifest = self._read_manifest()
        outputs = manifest.setdefault("outputs", [])
        if rel not in outputs:
            outputs.append(rel)
        manifest["version"] = int(manifest.get("version", 0)) + 1
        self._write_manifest(manifest)

    # -- convenience -----------------------------------------------------

    def list_files(self, subdir: str = "", pattern: str = "*") -> list[str]:
        """Recursively list files under ``subdir`` as sorted relative paths."""
        base = self.root if not subdir else self.resolve(subdir)
        return sorted(
            str(p.relative_to(self.root)).replace("\\", "/")
            for p in base.glob(f"**/{pattern}")
            if p.is_file()
        )


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
