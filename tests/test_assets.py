import tempfile
import unittest
from pathlib import Path

from geoai.server.assets import MARKED_URL, ensure_frontend_assets


class _Response:
    def __init__(self, contents: bytes):
        self.contents = contents

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit: int) -> bytes:
        self.limit = limit
        return self.contents


class FrontendAssetTests(unittest.TestCase):
    def test_downloads_missing_asset_atomically_into_vendor_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "vendor" / "marked.min.js"
            response = _Response(b"marked test bundle")
            requests = []

            def opener(request, timeout):
                requests.append((request, timeout))
                return response

            result = ensure_frontend_assets(destination, opener=opener)

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"marked test bundle")
            self.assertEqual(requests[0][0].full_url, MARKED_URL)
            self.assertEqual(requests[0][1], 30)
            self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

    def test_reuses_existing_asset_without_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "marked.min.js"
            destination.write_bytes(b"existing bundle")

            def opener(*args, **kwargs):
                raise AssertionError("existing assets should not be downloaded")

            result = ensure_frontend_assets(destination, opener=opener)

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"existing bundle")


if __name__ == "__main__":
    unittest.main()
