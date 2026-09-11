"""Download browser assets that are intentionally not checked into the repo."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

_STATIC_DIR = Path(__file__).parent / "static"
MARKED_VERSION = "15.0.12"
MARKED_URL = f"https://cdn.jsdelivr.net/npm/marked@{MARKED_VERSION}/marked.min.js"
MARKED_PATH = _STATIC_DIR / "vendor" / "marked.min.js"
_MAX_ASSET_BYTES = 2 * 1024 * 1024


def ensure_frontend_assets(
    asset_path: Path = MARKED_PATH,
    *,
    opener=urlopen,
) -> Path:
    """Ensure the locally served Marked bundle exists before starting Uvicorn.

    The file is downloaded to a sibling temporary file and atomically renamed,
    so a server restart cannot observe a partially written JavaScript bundle.
    Existing non-empty files are retained to support offline restarts.
    """
    if asset_path.is_file() and asset_path.stat().st_size:
        return asset_path

    asset_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        request = Request(
            MARKED_URL,
            headers={"User-Agent": "geo-ai-asset-bootstrap/1"},
        )
        with opener(request, timeout=30) as response:
            contents = response.read(_MAX_ASSET_BYTES + 1)
        if len(contents) > _MAX_ASSET_BYTES:
            raise RuntimeError(f"asset exceeds {_MAX_ASSET_BYTES} byte limit")
        if not contents:
            raise RuntimeError("download returned an empty response")

        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{asset_path.name}.",
            suffix=".tmp",
            dir=asset_path.parent,
            delete=False,
        ) as temporary:
            temporary.write(contents)
            temporary_path = temporary.name
        os.replace(temporary_path, asset_path)
        temporary_path = None
        return asset_path
    except Exception as exc:  # noqa: BLE001 - turn bootstrap failures into one clear error
        raise RuntimeError(
            f"Unable to download frontend asset marked@{MARKED_VERSION} from {MARKED_URL}. "
            "Check network access or place the asset at "
            f"{asset_path} before starting Geo-AI."
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
