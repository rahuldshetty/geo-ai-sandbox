"""In-memory cell model and its ``.ipynb`` (nbformat 4.5) serialization.

The server owns the notebook schema; no ``nbformat`` dependency. The internal
cell dict is JSON-serializable and carries a runtime-only ``status`` that is
derived (never serialized) on load.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

VALID_KINDS = frozenset({"markdown", "python", "prompt"})

_NBFORMAT = 4
_NBFORMAT_MINOR = 5


def new_cell(kind: str, source: str = "", index: int | None = None) -> dict:
    """Return a fresh cell dict (uuid4 id, status "idle").

    ``index`` is applied by the caller (``state.add_cell``); it is accepted here
    only for API parity and is otherwise unused.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid cell kind: {kind!r}")
    return {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "source": source,
        "outputs": [],
        "execution_count": None,
        "status": "idle",
    }


def _outputs_to_nb(outputs: list[dict]) -> list[dict]:
    """Convert internal (normalized) outputs to nbformat output objects."""
    result: list[dict] = []
    for out in outputs:
        if out.get("output_type") == "error":
            result.append(
                {
                    "output_type": "error",
                    "ename": out.get("ename"),
                    "evalue": out.get("evalue"),
                    "traceback": out.get("traceback") or [],
                }
            )
        else:
            result.append(
                {
                    "output_type": "stream",
                    "name": out.get("name") or "stdout",
                    "text": out.get("text") or "",
                }
            )
    return result


def _outputs_from_nb(nb_outputs: list[dict]) -> list[dict]:
    """Convert nbformat output objects to internal (normalized) outputs."""
    result: list[dict] = []
    for out in nb_outputs:
        if out.get("output_type") == "error":
            result.append(
                {
                    "output_type": "error",
                    "name": None,
                    "text": None,
                    "ename": out.get("ename"),
                    "evalue": out.get("evalue"),
                    "traceback": out.get("traceback"),
                }
            )
        elif out.get("output_type") == "stream":
            result.append(
                {
                    "output_type": "stream",
                    "name": out.get("name"),
                    "text": out.get("text"),
                    "ename": None,
                    "evalue": None,
                    "traceback": None,
                }
            )
        # Other nbformat output types are not produced by this harness and are
        # dropped (we own the schema).
    return result


def cell_to_nb(cell: dict) -> dict:
    """Map an internal cell to its nbformat 4.5 dict."""
    kind = cell["kind"]
    source_lines = cell.get("source", "").splitlines(keepends=True)
    if kind == "markdown":
        return {
            "cell_type": "markdown",
            "id": cell["id"],
            "metadata": {},
            "source": source_lines,
        }
    nb = {
        "cell_type": "code",
        "id": cell["id"],
        "metadata": {},
        "execution_count": cell.get("execution_count"),
        "outputs": _outputs_to_nb(cell.get("outputs", [])),
        "source": source_lines,
    }
    if kind == "prompt":
        nb["metadata"] = {"geoai": {"kind": "prompt"}}
    return nb


def nb_to_cell(nb_cell: dict) -> dict:
    """Map an nbformat cell to an internal cell dict (derives ``status``)."""
    cell_type = nb_cell.get("cell_type")
    if cell_type == "markdown":
        kind = "markdown"
    elif nb_cell.get("metadata", {}).get("geoai", {}).get("kind") == "prompt":
        kind = "prompt"
    else:
        kind = "python"

    outputs = _outputs_from_nb(nb_cell.get("outputs", []))
    execution_count = None if kind == "markdown" else nb_cell.get("execution_count")
    status = "done" if (outputs or execution_count is not None) else "idle"

    return {
        "id": nb_cell.get("id") or uuid.uuid4().hex,
        "kind": kind,
        "source": "".join(nb_cell.get("source", [])),
        "outputs": outputs,
        "execution_count": execution_count,
        "status": status,
    }


def read_nb(path: Path) -> list[dict]:
    """Load cells from ``path``; a missing file yields ``[]``."""
    if not path.exists():
        return []
    nb = json.loads(path.read_text(encoding="utf-8"))
    return [nb_to_cell(c) for c in nb.get("cells", [])]


def write_nb(path: Path, cells: list[dict]) -> None:
    """Write ``cells`` as an nbformat 4.5 JSON notebook (mkdir parents)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    nb = {
        "cells": [cell_to_nb(c) for c in cells],
        "metadata": {},
        "nbformat": _NBFORMAT,
        "nbformat_minor": _NBFORMAT_MINOR,
    }
    path.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
