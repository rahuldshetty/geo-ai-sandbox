import tempfile
import unittest
from pathlib import Path

from geoai.context import GeoContext, set_context
from geoai.skills.map_tools import fit_bounds
from geoai.workspace import Workspace


class MapToolTests(unittest.TestCase):
    def test_fit_bounds_confirms_resulting_view(self):
        class FakeMap:
            def __init__(self):
                self.bounds = None
                self.project = {"mapView": None}

            def fit_project_bounds(self, bounds):
                self.bounds = list(bounds)
                self.project["mapView"] = {
                    "bbox": self.bounds,
                    "center": [85.5, 27.5],
                    "zoom": 10,
                }

            def save_project(self, path):
                Path(path).write_text("{}", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "workspace").create()
            fake_map = FakeMap()
            set_context(GeoContext(map=fake_map, workspace=workspace))
            try:
                result = fit_bounds([85.0, 27.0, 86.0, 28.0])
            finally:
                set_context(None)

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["mapView"]["bbox"], [85.0, 27.0, 86.0, 28.0])


if __name__ == "__main__":
    unittest.main()
