"""Vector service and pack: GeoPandas processing, map layers, and confinement."""

import json
import tempfile
import unittest
import warnings
from pathlib import Path

from spatial_intelligence.contracts.effects import Effect
from spatial_intelligence.contracts.errors import RuntimeNotBoundError, WorkspaceError
from spatial_intelligence.geo import vector
from spatial_intelligence.map import document
from spatial_intelligence.tools import ToolRegistry, ToolRuntime
from spatial_intelligence.tools.packs.vector import VectorPack
from spatial_intelligence.tools.runtime import RuntimeEvents
from spatial_intelligence.workspace import Workspace

SIDE = 0.5


def square(x: float) -> dict:
    """Return a closed square of side ``SIDE`` with its lower-left at ``(x, 0)``."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [x, 0.0],
                [x + SIDE, 0.0],
                [x + SIDE, SIDE],
                [x, SIDE],
                [x, 0.0],
            ]
        ],
    }


def feature(name: str, value: float, geometry: dict) -> dict:
    return {
        "type": "Feature",
        "properties": {"name": name, "value": value},
        "geometry": geometry,
    }


#: Four squares in a row, so a clip against the first one keeps fewer features.
POINTS = {
    "type": "FeatureCollection",
    "features": [
        feature("a", 1.0, square(0.0)),
        feature("b", 2.0, square(1.0)),
        feature("c", 3.0, square(2.0)),
        feature("d", 4.0, square(3.0)),
    ],
}
#: Covers only square "a" (x from -0.1 to 0.4).
MASK = {
    "type": "FeatureCollection",
    "features": [feature("mask", 0.0, square(-0.1))],
}
SQUARE_ONE = {
    "type": "FeatureCollection",
    "features": [feature("a", 1.0, square(0.0))],
}


class VectorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self._tmp.name) / "workspace").create()
        self.write("data/points.geojson", POINTS)
        self.write("data/square.geojson", SQUARE_ONE)
        self.write("data/mask.geojson", MASK)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative: str, payload: dict) -> Path:
        target = self.workspace.resolve(relative, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def outputs(self) -> list[str]:
        manifest = json.loads(
            (self.workspace.root / "workspace.json").read_text(encoding="utf-8")
        )
        return manifest["outputs"]


class VectorServiceTests(VectorTestCase):
    def test_read_vector_describes_the_dataset(self):
        info = vector.read_vector(self.workspace, "data/points.geojson")

        self.assertEqual(info["crs"], "EPSG:4326")
        self.assertEqual(info["columns"], ["name", "value", "geometry"])
        self.assertEqual(info["len"], 4)
        self.assertEqual(info["bounds"], [0.0, 0.0, 3.5, 0.5])
        self.assertEqual(info["geom_types"], ["Polygon"])

    def test_reproject_vector_changes_the_crs_of_the_written_output(self):
        written = vector.reproject_vector(
            self.workspace, "data/points.geojson", "results/web.geojson", "EPSG:3857"
        )

        self.assertEqual(self.workspace.relative(written), "results/web.geojson")
        self.assertEqual(
            vector.read_vector(self.workspace, "results/web.geojson")["crs"], "EPSG:3857"
        )
        self.assertEqual(
            vector.read_vector(self.workspace, "data/points.geojson")["crs"], "EPSG:4326"
        )

    def test_buffer_grows_the_geometry_and_keeps_the_source_crs(self):
        # 500 m in UTM must stay a sub-degree change in the stored EPSG:4326
        # layer, which is what tells metric buffering apart from degree buffering.
        written = vector.buffer(
            self.workspace, "data/square.geojson", "results/buffered.geojson", 500.0
        )
        before = vector.read_vector(self.workspace, "data/square.geojson")
        after = vector.read_vector(self.workspace, "results/buffered.geojson")

        self.assertEqual(self.workspace.relative(written), "results/buffered.geojson")
        self.assertEqual(after["crs"], before["crs"])
        self.assertEqual(after["geom_types"], ["Polygon"])
        self.assertLess(after["bounds"][0], before["bounds"][0])
        self.assertLess(after["bounds"][1], before["bounds"][1])
        self.assertGreater(after["bounds"][2], before["bounds"][2])
        self.assertGreater(after["bounds"][3], before["bounds"][3])
        self.assertLess(after["bounds"][2] - after["bounds"][0], 1.0)

    def test_buffer_in_degrees_uses_the_layer_units(self):
        # Buffering a geographic layer in its own units is what GeoPandas warns
        # about; that is exactly the documented non-metric behaviour.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            written = vector.buffer(
                self.workspace,
                "data/square.geojson",
                "results/wide.geojson",
                1.0,
                unit="degrees",
            )
        after = vector.read_vector(self.workspace, self.workspace.relative(written))

        self.assertLess(after["bounds"][0], -0.9)
        self.assertGreater(after["bounds"][2], 1.4)

    def test_clip_vector_keeps_only_the_intersecting_features(self):
        written = vector.clip_vector(
            self.workspace, "data/points.geojson", "results/clipped.geojson", "data/mask.geojson"
        )
        clipped = vector.read_vector(self.workspace, "results/clipped.geojson")

        self.assertEqual(self.workspace.relative(written), "results/clipped.geojson")
        self.assertEqual(clipped["len"], 1)
        self.assertLess(clipped["len"], vector.read_vector(self.workspace, "data/points.geojson")["len"])

    def test_to_geojson_writes_a_valid_feature_collection(self):
        written = vector.to_geojson(
            self.workspace, "data/points.geojson", "results/points.geojson"
        )
        payload = json.loads(written.read_text(encoding="utf-8"))

        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertEqual(len(payload["features"]), 4)
        self.assertEqual(payload["features"][0]["geometry"]["type"], "Polygon")
        self.assertEqual(payload["features"][0]["properties"]["name"], "a")

    def test_reading_a_missing_file_is_refused(self):
        with self.assertRaises(WorkspaceError):
            vector.read_vector(self.workspace, "data/missing.geojson")

    def test_writers_are_confined_to_output_directories(self):
        with self.assertRaises(WorkspaceError):
            vector.to_geojson(self.workspace, "data/points.geojson", "traces/points.geojson")
        with self.assertRaises(WorkspaceError):
            vector.reproject_vector(
                self.workspace, "data/points.geojson", "../escaped.geojson", "EPSG:3857"
            )
        with self.assertRaises(WorkspaceError):
            vector.buffer(
                self.workspace,
                "data/points.geojson",
                str(Path(self._tmp.name) / "absolute.geojson"),
                10.0,
            )


class VectorMapTests(VectorTestCase):
    def test_add_vector_to_map_returns_the_layer_id(self):
        m = document.create_map(self.workspace)

        layer_id = vector.add_vector_to_map(
            self.workspace, m, "data/points.geojson", "Points", "value"
        )

        self.assertTrue(layer_id)
        self.assertEqual([layer["id"] for layer in m.project["layers"]], [layer_id])
        self.assertEqual([layer["name"] for layer in m.project["layers"]], ["Points"])


class VectorPackTests(VectorTestCase):
    def build_pack(self, notifications: list[str], map_obj=None) -> ToolRegistry:
        runtime = ToolRuntime(
            workspace=self.workspace,
            map=map_obj,
            events=RuntimeEvents(
                files_changed=lambda: notifications.append("files"),
                map_changed=lambda: notifications.append("map"),
            ),
        )
        registry = ToolRegistry()
        registry.add_pack(VectorPack, runtime)
        return registry

    def test_writers_record_the_output_and_notify_files(self):
        notifications: list[str] = []
        registry = self.build_pack(notifications)

        absolute = registry.get("reproject_vector").callable(
            "data/points.geojson", "results/web.geojson", "EPSG:3857"
        )

        self.assertEqual(Path(absolute), self.workspace.resolve("results/web.geojson"))
        self.assertEqual(notifications, ["files"])
        self.assertEqual(self.outputs(), ["results/web.geojson"])

    def test_read_vector_reports_a_missing_file(self):
        with self.assertRaises(WorkspaceError):
            self.build_pack([]).get("read_vector").callable("data/missing.geojson")

    def test_writing_outside_the_output_directories_is_refused(self):
        registry = self.build_pack([])

        with self.assertRaises(WorkspaceError):
            registry.get("to_geojson").callable("data/points.geojson", "traces/points.geojson")

        self.assertEqual(self.outputs(), [])

    def test_add_vector_to_map_persists_the_snapshot_and_notifies_map(self):
        notifications: list[str] = []
        m = document.create_map(self.workspace)
        registry = self.build_pack(notifications, map_obj=m)

        layer_id = registry.get("add_vector_to_map").callable(
            "data/points.geojson", "Points", "value"
        )

        snapshot = document.snapshot_path(self.workspace)
        self.assertTrue(snapshot.is_file())
        project = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual([layer["id"] for layer in project["layers"]], [layer_id])
        self.assertEqual(
            [layer["name"] for layer in project["layers"]], ["Points"]
        )
        self.assertEqual(notifications, ["map"])

    def test_add_vector_to_map_requires_a_live_map(self):
        with self.assertRaises(RuntimeNotBoundError):
            self.build_pack([]).get("add_vector_to_map").callable(
                "data/points.geojson", "Points"
            )

    def test_registered_vector_tools_expose_expected_effects(self):
        registry = self.build_pack([])

        self.assertEqual(
            sorted(registry.names()),
            [
                "add_vector_to_map",
                "buffer",
                "clip_vector",
                "read_vector",
                "reproject_vector",
                "to_geojson",
            ],
        )
        self.assertTrue(registry.replay_safe("read_vector"))
        self.assertFalse(registry.replay_safe("buffer"))
        self.assertFalse(registry.replay_safe("to_geojson"))
        self.assertFalse(registry.replay_safe("add_vector_to_map"))
        self.assertEqual(registry.get("read_vector").effects, frozenset({Effect.READ}))
        self.assertEqual(
            registry.get("add_vector_to_map").effects, frozenset({Effect.MAP_WRITE})
        )
        self.assertEqual(registry.get("buffer").category, "vector")


if __name__ == "__main__":
    unittest.main()
