"""Map document, layer services, and the layers pack.

The live map is real (a headless ``geolibre.Map``), so these tests prove the
observable contract: ids returned, snapshots that reload, re-pointed local
sources, registry effects, and the UI notification a map mutation raises.
"""

import json
import tempfile
import unittest
from pathlib import Path

from spatial_intelligence.contracts.effects import Effect
from spatial_intelligence.contracts.errors import ToolInputError, WorkspaceError
from spatial_intelligence.map import document
from spatial_intelligence.map import layers as layerops
from spatial_intelligence.tools import ToolRegistry, ToolRuntime
from spatial_intelligence.tools.packs.layers import LayersPack
from spatial_intelligence.tools.runtime import RuntimeEvents
from spatial_intelligence.workspace import Workspace

BASE_URL = "http://127.0.0.1:8123"
FEATURE_COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"name": "a", "value": 3},
            "geometry": {"type": "Point", "coordinates": [7.0, 45.0]},
        }
    ],
}


def url_for(rel: str) -> str:
    return f"{BASE_URL}/api/files/{rel}"


class MapTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Workspace(Path(self._tmp.name) / "workspace").create()
        self.map = document.create_map(self.workspace)

    def write(self, relative: str, content: str | bytes) -> Path:
        path = self.workspace.resolve(relative, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def geojson_file(self, relative: str) -> Path:
        return self.write(relative, json.dumps(FEATURE_COLLECTION))

    def reload_map(self):
        """A fresh map restored from the workspace snapshot."""
        return document.create_map(self.workspace)


class MapDocumentTests(MapTestCase):
    def test_snapshot_name_and_path_are_frozen(self):
        self.assertEqual(document.SNAPSHOT_NAME, "current.geolibre.json")
        self.assertEqual(
            document.snapshot_path(self.workspace),
            self.workspace.maps / "current.geolibre.json",
        )

    def test_create_map_starts_empty_when_no_snapshot_exists(self):
        summary = self.map.describe()

        self.assertEqual(summary["layerCount"], 0)
        self.assertEqual(summary["mapView"]["center"], [0.0, 0.0])
        self.assertEqual(summary["mapView"]["zoom"], 2.0)

    def test_persist_map_writes_the_snapshot_and_bumps_the_version(self):
        before = self.workspace._read_manifest()["version"]

        document.persist_map(self.map, self.workspace)

        snapshot = document.snapshot_path(self.workspace)
        project = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(project["layers"], [])
        self.assertEqual(self.workspace._read_manifest()["version"], before + 1)

    def test_a_new_map_restores_the_persisted_layers(self):
        self.geojson_file("data/points.geojson")
        layer_id = layerops.add_geojson(
            self.workspace, self.map, "data/points.geojson", "Points"
        )

        restored = self.reload_map()

        self.assertEqual(restored.describe()["layerCount"], 1)
        self.assertEqual(layerops.find_layer(restored, layer_id)["name"], "Points")

    def test_load_project_replaces_the_live_project(self):
        project = self.map.to_project()
        project["name"] = "Replaced"

        document.load_project(self.map, project)

        self.assertEqual(self.map.describe()["name"], "Replaced")


class MapLayerServiceTests(MapTestCase):
    def test_add_geojson_returns_an_id_and_the_snapshot_reloads_it(self):
        self.geojson_file("data/points.geojson")

        layer_id = layerops.add_geojson(
            self.workspace, self.map, "data/points.geojson", "Points"
        )

        self.assertIsInstance(layer_id, str)
        self.assertTrue(layer_id)
        layer = layerops.find_layer(self.map, layer_id)
        self.assertEqual(layer["name"], "Points")
        self.assertEqual(layerops.find_layer(self.reload_map(), layer_id)["name"], "Points")

    def test_add_geojson_accepts_a_literal_and_rejects_a_missing_file(self):
        layer_id = layerops.add_geojson(
            self.workspace, self.map, json.dumps(FEATURE_COLLECTION), "Literal"
        )

        self.assertTrue(layerops.find_layer(self.map, layer_id))
        with self.assertRaises(WorkspaceError):
            layerops.add_geojson(self.workspace, self.map, "data/missing.geojson", "Nope")

    def test_add_vector_reads_a_local_file_into_the_snapshot(self):
        self.geojson_file("data/roads.geojson")

        layer_id = layerops.add_vector(
            self.workspace, self.map, "data/roads.geojson", "Roads"
        )

        self.assertEqual(layerops.find_layer(self.map, layer_id)["name"], "Roads")
        self.assertEqual(layerops.find_layer(self.reload_map(), layer_id)["name"], "Roads")

    def test_add_vector_accepts_a_remote_url_without_touching_the_disk(self):
        layer_id = layerops.add_vector(
            self.workspace, self.map, "https://example.com/roads.fgb", "Remote", data_format="flatgeobuf"
        )

        self.assertEqual(
            layerops.find_layer(self.map, layer_id)["source"]["url"],
            "https://example.com/roads.fgb",
        )

    def test_add_raster_embeds_the_serving_url_and_tags_the_workspace_path(self):
        self.write("data/dem.tif", b"not-a-real-cog")

        layer_id = layerops.add_raster(
            self.workspace, self.map, "data/dem.tif", "DEM", file_url=url_for
        )

        layer = layerops.find_layer(self.map, layer_id)
        self.assertEqual(layer["sourcePath"], url_for("data/dem.tif"))
        self.assertEqual(layer["metadata"][layerops.LOCAL_SOURCE_KEY], "data/dem.tif")
        reloaded = layerops.find_layer(self.reload_map(), layer_id)
        self.assertEqual(reloaded["sourcePath"], url_for("data/dem.tif"))
        self.assertEqual(reloaded["metadata"][layerops.LOCAL_SOURCE_KEY], "data/dem.tif")

    def test_add_raster_passes_a_remote_url_through_untagged(self):
        layer_id = layerops.add_raster(
            self.workspace, self.map, "https://example.com/dem.tif", "Remote DEM"
        )

        layer = layerops.find_layer(self.map, layer_id)
        self.assertEqual(layer["sourcePath"], "https://example.com/dem.tif")
        self.assertNotIn(layerops.LOCAL_SOURCE_KEY, layer.get("metadata") or {})

    def test_add_raster_validates_a_local_path_before_using_it(self):
        with self.assertRaises(WorkspaceError):
            layerops.add_raster(
                self.workspace, self.map, "data/missing.tif", "DEM", file_url=url_for
            )

    def test_repoint_rewrites_a_legacy_session_url_and_restores_the_tag(self):
        self.write("results/legacy.tif", b"legacy")
        legacy_url = "http://127.0.0.1:9999/_geolibre_local/deadbeef/results/legacy.tif"
        layer_id = self.map.add_raster(legacy_url, "Legacy")
        layerops.find_layer(self.map, layer_id)["sourcePath"] = legacy_url
        document.persist_map(self.map, self.workspace)

        restored = self.reload_map()
        count = layerops.repoint_local_rasters(restored, self.workspace, url_for)
        document.persist_map(restored, self.workspace)

        self.assertEqual(count, 1)
        layer = layerops.find_layer(restored, layer_id)
        self.assertEqual(layer["source"]["url"], url_for("results/legacy.tif"))
        self.assertEqual(layer["sourcePath"], url_for("results/legacy.tif"))
        self.assertEqual(layer["metadata"][layerops.LOCAL_SOURCE_KEY], "results/legacy.tif")
        reloaded = layerops.find_layer(self.reload_map(), layer_id)
        self.assertEqual(reloaded["source"]["url"], url_for("results/legacy.tif"))

    def test_repoint_moves_a_stale_api_files_url_to_the_current_origin(self):
        self.write("data/scene.tif", b"scene")
        stale = "http://127.0.0.1:8000/api/files/data/scene.tif"
        layer_id = self.map.add_raster(stale, "Scene")
        layerops.set_layer_metadata(
            self.map, layer_id, layerops.LOCAL_SOURCE_KEY, "data/scene.tif"
        )

        count = layerops.repoint_local_rasters(self.map, self.workspace, url_for)

        self.assertEqual(count, 1)
        self.assertEqual(
            layerops.find_layer(self.map, layer_id)["sourcePath"], url_for("data/scene.tif")
        )

    def test_repoint_leaves_remote_layers_alone(self):
        layer_id = self.map.add_raster("https://example.com/remote.tif", "Remote")

        count = layerops.repoint_local_rasters(self.map, self.workspace, url_for)

        self.assertEqual(count, 0)
        self.assertEqual(
            layerops.find_layer(self.map, layer_id)["sourcePath"],
            "https://example.com/remote.tif",
        )

    def test_describe_reports_the_layers_it_holds(self):
        self.geojson_file("data/points.geojson")
        layer_id = layerops.add_geojson(
            self.workspace, self.map, "data/points.geojson", "Points"
        )

        summary = layerops.describe(self.map)

        self.assertEqual(summary["layerCount"], 1)
        self.assertEqual([layer["id"] for layer in summary["layers"]], [layer_id])

    def test_set_view_applies_center_and_zoom(self):
        result = layerops.set_view(self.workspace, self.map, center=[7.5, 45.5], zoom=9)

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["mapView"]["center"], [7.5, 45.5])
        self.assertEqual(result["mapView"]["zoom"], 9.0)
        self.assertEqual(self.reload_map().project["mapView"]["zoom"], 9.0)

    def test_style_layer_reports_an_unknown_layer(self):
        with self.assertRaises(ToolInputError):
            layerops.style_layer(self.workspace, self.map, "nope", {"fillColor": "#fff"})

    def test_clear_layers_empties_the_persisted_snapshot(self):
        self.geojson_file("data/points.geojson")
        layerops.add_geojson(self.workspace, self.map, "data/points.geojson", "Points")

        result = layerops.clear_layers(self.workspace, self.map)

        self.assertEqual(result["layerCount"], 0)
        self.assertEqual(self.reload_map().describe()["layerCount"], 0)

    def test_list_colormaps_returns_ramps_the_map_accepts(self):
        ramps = layerops.list_colormaps()

        self.assertEqual(ramps["viridis"][0], "#440154")
        self.assertGreater(len(ramps["viridis"]), 1)


class LayersPackTests(MapTestCase):
    def build_pack(self, notifications: list[str], map_notifications: list[str]):
        runtime = ToolRuntime(
            workspace=self.workspace,
            map=self.map,
            base_url=f"{BASE_URL}/",
            events=RuntimeEvents(
                files_changed=lambda: notifications.append("files"),
                map_changed=lambda: map_notifications.append("map"),
            ),
        )
        registry = ToolRegistry()
        registry.add_pack(LayersPack, runtime)
        return registry

    def test_the_registered_map_tools_carry_their_documented_effects(self):
        registry = self.build_pack([], [])
        expected = {
            "describe_map": frozenset({Effect.READ}),
            "describe_geolibre_bridge": frozenset({Effect.READ}),
            "list_colormaps": frozenset({Effect.READ}),
            "add_geojson": frozenset({Effect.MAP_WRITE}),
            "add_vector": frozenset({Effect.MAP_WRITE}),
            "add_raster": frozenset({Effect.MAP_WRITE}),
            "add_tile_layer": frozenset({Effect.MAP_WRITE}),
            "add_wms": frozenset({Effect.MAP_WRITE}),
            "set_view": frozenset({Effect.MAP_WRITE}),
            "set_basemap": frozenset({Effect.MAP_WRITE}),
            "fit_bounds": frozenset({Effect.MAP_WRITE}),
            "style_layer": frozenset({Effect.MAP_WRITE}),
            "classify_layer": frozenset({Effect.MAP_WRITE}),
            "set_layer_visibility": frozenset({Effect.MAP_WRITE}),
            "set_layer_opacity": frozenset({Effect.MAP_WRITE}),
            "remove_layer": frozenset({Effect.MAP_WRITE}),
            "clear_layers": frozenset({Effect.MAP_WRITE}),
            "add_legend": frozenset({Effect.MAP_WRITE}),
            "add_colorbar": frozenset({Effect.MAP_WRITE}),
            "save_map": frozenset({Effect.WORKSPACE_WRITE}),
            "export_html": frozenset({Effect.WORKSPACE_WRITE}),
        }

        self.assertEqual(registry.names(), list(expected))
        for name, effects in expected.items():
            self.assertEqual(registry.get(name).effects, effects, name)
        self.assertEqual(
            registry.core_names(), {"describe_map", "describe_geolibre_bridge"}
        )
        self.assertNotIn("zoom_to_layer", registry)
        self.assertEqual(registry.categories(), {"layers": tuple(expected)})

    def test_a_map_write_tool_notifies_the_map_hook(self):
        map_notifications: list[str] = []
        registry = self.build_pack([], map_notifications)
        self.geojson_file("data/points.geojson")

        layer_id = registry.get("add_geojson").callable("data/points.geojson", "Points")

        self.assertEqual(map_notifications, ["map"])
        self.assertTrue(layerops.find_layer(self.map, layer_id))
        self.assertEqual(self.reload_map().describe()["layerCount"], 1)

    def test_read_only_tools_leave_the_map_hook_alone(self):
        map_notifications: list[str] = []
        file_notifications: list[str] = []
        registry = self.build_pack(file_notifications, map_notifications)

        summary = registry.get("describe_map").callable()
        bridge = registry.get("describe_geolibre_bridge").callable()
        ramps = registry.get("list_colormaps").callable()

        self.assertEqual(summary["layerCount"], 0)
        self.assertFalse(bridge["connected"])
        self.assertIn("viridis", ramps)
        self.assertEqual(map_notifications, [])
        self.assertEqual(file_notifications, [])

    def test_describe_map_reports_a_layer_added_through_the_pack(self):
        registry = self.build_pack([], [])
        self.geojson_file("data/points.geojson")
        layer_id = registry.get("add_geojson").callable("data/points.geojson", "Points")

        summary = registry.get("describe_map").callable()

        self.assertEqual(summary["layerCount"], 1)
        self.assertEqual(summary["layers"][0]["id"], layer_id)

    def test_add_raster_through_the_pack_uses_the_serving_route(self):
        registry = self.build_pack([], [])
        self.write("data/dem.tif", b"not-a-real-cog")

        layer_id = registry.get("add_raster").callable("data/dem.tif", "DEM")

        self.assertEqual(
            layerops.find_layer(self.map, layer_id)["sourcePath"], url_for("data/dem.tif")
        )

    def test_save_map_records_the_output_without_touching_the_live_map(self):
        file_notifications: list[str] = []
        map_notifications: list[str] = []
        registry = self.build_pack(file_notifications, map_notifications)

        saved = registry.get("save_map").callable("park.geolibre.json")

        self.assertEqual(Path(saved), self.workspace.maps / "park.geolibre.json")
        self.assertTrue(Path(saved).is_file())
        self.assertIn("maps/park.geolibre.json", self.workspace._read_manifest()["outputs"])
        self.assertEqual(file_notifications, ["files"])
        self.assertEqual(map_notifications, [])

    def test_export_html_records_the_output_without_touching_the_live_map(self):
        file_notifications: list[str] = []
        map_notifications: list[str] = []
        registry = self.build_pack(file_notifications, map_notifications)

        exported = registry.get("export_html").callable("map.html", "Field Map")

        self.assertEqual(Path(exported), self.workspace.results / "map.html")
        self.assertIn("Field Map", Path(exported).read_text(encoding="utf-8"))
        self.assertIn("results/map.html", self.workspace._read_manifest()["outputs"])
        self.assertEqual(map_notifications, [])

    def test_view_and_layer_settings_persist_through_the_pack(self):
        registry = self.build_pack([], [])
        self.geojson_file("data/points.geojson")
        layer_id = registry.get("add_geojson").callable("data/points.geojson", "Points")

        registry.get("set_layer_opacity").callable(layer_id, 0.25)
        registry.get("set_layer_visibility").callable(layer_id, False)
        registry.get("set_view").callable([7.0, 45.0], 6)

        restored = self.reload_map()
        layer = layerops.find_layer(restored, layer_id)
        self.assertEqual(layer["opacity"], 0.25)
        self.assertFalse(layer["visible"])
        self.assertEqual(restored.project["mapView"]["center"], [7.0, 45.0])

    def test_removing_a_layer_removes_it_from_the_snapshot(self):
        registry = self.build_pack([], [])
        self.geojson_file("data/points.geojson")
        layer_id = registry.get("add_geojson").callable("data/points.geojson", "Points")

        removed = registry.get("remove_layer").callable(layer_id)

        self.assertEqual(removed["status"], "applied")
        self.assertIsNone(layerops.find_layer(self.reload_map(), layer_id))


if __name__ == "__main__":
    unittest.main()
