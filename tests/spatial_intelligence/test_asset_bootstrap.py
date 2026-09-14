"""The front-end asset bootstrap: download once, atomically, offline-friendly."""

import tempfile
import unittest
from pathlib import Path

from spatial_intelligence.server.assets import (
    MARKED_PATH,
    MARKED_URL,
    MAX_ASSET_BYTES,
    ensure_frontend_assets,
)


class FakeResponse:
    def __init__(self, contents: bytes):
        self.contents = contents
        self.limit: int | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit: int) -> bytes:
        self.limit = limit
        return self.contents


class FrontendAssetTests(unittest.TestCase):
    def test_the_pinned_bundle_path_sits_under_the_package_web_directory(self):
        self.assertEqual(MARKED_PATH.name, "marked.min.js")
        self.assertEqual(MARKED_PATH.parent.name, "vendor")
        self.assertEqual(MARKED_PATH.parent.parent.name, "web")
        self.assertIn("15.0.12", MARKED_URL)

    def test_downloads_a_missing_asset_atomically_into_the_vendor_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "vendor" / "marked.min.js"
            response = FakeResponse(b"marked test bundle")
            requests = []

            def opener(request, timeout):
                requests.append((request, timeout))
                return response

            result = ensure_frontend_assets(destination, opener=opener)

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"marked test bundle")
            self.assertEqual(requests[0][0].full_url, MARKED_URL)
            self.assertEqual(requests[0][1], 30)
            # The temporary file is renamed into place, never left behind.
            self.assertEqual(list(destination.parent.glob(".*.tmp")), [])
            self.assertEqual(response.limit, MAX_ASSET_BYTES + 1)

    def test_reuses_an_existing_asset_without_touching_the_network(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "marked.min.js"
            destination.write_bytes(b"existing bundle")

            def opener(*args, **kwargs):
                raise AssertionError("an existing asset must not be downloaded")

            result = ensure_frontend_assets(destination, opener=opener)

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"existing bundle")

    def test_an_empty_file_is_treated_as_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "marked.min.js"
            destination.write_bytes(b"")

            result = ensure_frontend_assets(
                destination, opener=lambda request, timeout: FakeResponse(b"fresh")
            )

            self.assertEqual(result.read_bytes(), b"fresh")

    def test_an_oversized_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "marked.min.js"

            with self.assertRaises(RuntimeError):
                ensure_frontend_assets(
                    destination,
                    opener=lambda request, timeout: FakeResponse(
                        b"x" * (MAX_ASSET_BYTES + 5)
                    ),
                )

            self.assertFalse(destination.exists())

    def test_a_download_failure_names_the_asset_and_the_target_path(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "marked.min.js"

            def explode(request, timeout):
                raise OSError("connection reset")

            with self.assertRaises(RuntimeError) as caught:
                ensure_frontend_assets(destination, opener=explode)

            message = str(caught.exception)
            self.assertIn("marked@15.0.12", message)
            self.assertIn(str(destination), message)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
