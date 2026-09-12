import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from geoai.context import GeoContext, set_context
from geoai.skills.workspace_tools import download, download_files
from geoai.workspace import Workspace


class _Response:
    def __init__(self, body: bytes):
        self._body = body
        self._offset = 0
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        if size < 0:
            size = len(self._body)
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class DownloadToolTests(unittest.TestCase):
    def test_download_emits_progress_and_records_data_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "workspace").create()
            events = []
            set_context(
                GeoContext(
                    map=None,
                    workspace=workspace,
                    download_progress=events.append,
                    download_parent_id="prompt-1",
                )
            )
            try:
                with patch(
                    "geoai.skills.workspace_tools.urllib.request.urlopen",
                    return_value=_Response(b"asset"),
                ):
                    path = download("https://example.com/asset.tif", "scene.tif")
                self.assertEqual((workspace.root / path).read_bytes(), b"asset")
                self.assertEqual(events[0]["status"], "running")
                self.assertEqual(events[-1]["status"], "done")
                self.assertEqual(events[-1]["parent_cell_id"], "prompt-1")
                self.assertEqual(events[-1]["path"], "data/scene.tif")
            finally:
                set_context(None)

    def test_download_files_runs_independent_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "workspace").create()
            events = []
            set_context(GeoContext(map=None, workspace=workspace, download_progress=events.append))
            try:
                def open_url(request, timeout=0):
                    return _Response(request.full_url.encode("utf-8"))

                with patch(
                    "geoai.skills.workspace_tools.urllib.request.urlopen",
                    side_effect=open_url,
                ):
                    paths = download_files(
                        [
                            {"url": "https://example.com/one.tif"},
                            {"url": "https://example.com/two.tif"},
                        ]
                    )
                self.assertEqual(len(paths), 2)
                self.assertEqual({Path(item["path"]).name for item in paths}, {"one.tif", "two.tif"})
                self.assertEqual({item["status"] for item in paths}, {"done"})
                done = {event["filename"] for event in events if event["status"] == "done"}
                self.assertEqual(done, {"one.tif", "two.tif"})
            finally:
                set_context(None)


if __name__ == "__main__":
    unittest.main()
