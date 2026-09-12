import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import geoai.skills.catalog_tools as catalog_tools
from geoai.context import GeoContext, set_context
from geoai.skills.catalog_tools import (
    add_catalog_scene,
    search_openaerialmap,
    search_vantor_events,
    search_vantor_imagery,
)
from geoai.workspace import Workspace


class CatalogToolTests(unittest.TestCase):
    @patch("geoai.skills.catalog_tools._fetch_json")
    def test_vantor_event_search_tolerates_natural_query(self, fetch):
        fetch.return_value = {
            "links": [
                {
                    "rel": "child",
                    "href": "Nepal-Flooding-Aug-2026/catalog.json",
                    "title": "Nepal-Flooding-Aug-2026",
                },
                {
                    "rel": "child",
                    "href": "Other-Earthquake/catalog.json",
                    "title": "Other-Earthquake",
                },
            ]
        }

        found = search_vantor_events("load Nepal flood data")

        self.assertEqual([event["id"] for event in found], ["Nepal-Flooding-Aug-2026"])

    @patch("geoai.skills.catalog_tools._fetch_json")
    def test_vantor_scenes_are_normalized_and_filtered(self, fetch):
        def response(url, **kwargs):
            if url.endswith("/events/catalog.json"):
                return {
                    "links": [
                        {
                            "rel": "child",
                            "href": "Nepal/catalog.json",
                            "title": "Nepal",
                        }
                    ]
                }
            if url.endswith("/Nepal/catalog.json"):
                return {"links": [{"rel": "item", "href": "post.json"}]}
            return {
                "id": "scene-1",
                "bbox": [85.0, 27.0, 86.0, 28.0],
                "properties": {
                    "datetime": "2026-08-27T00:00:00Z",
                    "phase": "post-event",
                    "vehicle_name": "WV03",
                    "eo:cloud_cover": 5,
                    "pan_gsd": 0.3,
                },
                "assets": {
                    "visual": {
                        "href": "https://example.com/post.tif",
                        "type": "image/tiff",
                    },
                    "thumbnail": {"href": "https://example.com/post.jpg"},
                },
            }

        fetch.side_effect = response

        scenes = search_vantor_imagery(
            "Nepal",
            bounds=[85.2, 27.2, 85.8, 27.8],
            phase="post",
        )

        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["sensor"], "WV03")
        self.assertEqual(scenes[0]["render"], "cog")
        self.assertTrue(scenes[0]["scene_key"].startswith("vantor:scene-1:"))

    @patch("geoai.skills.catalog_tools._fetch_json")
    def test_openaerialmap_uses_geolibre_titiler_contract(self, fetch):
        fetch.return_value = {
            "meta": {"found": "1"},
            "results": [
                {
                    "_id": "oam-1",
                    "uuid": "https://example.com/aerial.tif",
                    "title": "Flood survey",
                    "provider": "Example",
                    "acquisition_end": "2026-08-28T00:00:00Z",
                    "bbox": [85.0, 27.0, 86.0, 28.0],
                    "properties": {"thumbnail": "https://example.com/thumb.png"},
                }
            ],
        }

        result = search_openaerialmap([85.0, 27.0, 86.0, 28.0])

        self.assertEqual(result["found"], 1)
        self.assertIn("titiler.hotosm.org", result["scenes"][0]["tile_url"])
        self.assertEqual(result["scenes"][0]["render"], "xyz")

    @patch("geoai.skills.catalog_tools.download_catalog_scene")
    @patch("geoai.skills.catalog_tools._fetch_json")
    def test_scene_cache_survives_memory_reset_and_records_layer_source(
        self, fetch, download_scene
    ):
        fetch.return_value = {
            "meta": {"found": 1},
            "results": [
                {
                    "_id": "oam-persisted",
                    "uuid": "https://example.com/aerial.tif",
                    "title": "Persisted scene",
                    "properties": {},
                }
            ],
        }

        class FakeMap:
            def __init__(self):
                self.project = {"layers": []}

            def add_raster(self, path, name, **kwargs):
                self.project["layers"].append({"id": "layer-1", "name": name, "path": path})
                return "layer-1"

            def save_project(self, path):
                Path(path).write_text("{}", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "workspace").create()
            local_asset = workspace.data / "aerial.tif"
            local_asset.write_bytes(b"COG")
            download_scene.return_value = str(local_asset)
            fake_map = FakeMap()
            set_context(GeoContext(map=fake_map, workspace=workspace))
            try:
                scene = search_openaerialmap([85.0, 27.0, 86.0, 28.0])["scenes"][0]
                catalog_tools._scene_cache.clear()
                layer_id = add_catalog_scene(scene["scene_key"])
                repeated_layer_id = add_catalog_scene(scene["scene_key"])
            finally:
                set_context(None)

        self.assertEqual(layer_id["status"], "added")
        self.assertEqual(layer_id["layer_id"], "layer-1")
        self.assertEqual(repeated_layer_id["status"], "existing")
        self.assertEqual(repeated_layer_id["layer_id"], "layer-1")
        self.assertEqual(repeated_layer_id["name"], "Persisted scene")
        self.assertEqual(len(fake_map.project["layers"]), 1)
        metadata = fake_map.project["layers"][0]["metadata"]["geoaiCatalog"]
        self.assertEqual(metadata["id"], "oam-persisted")
        self.assertEqual(metadata["asset_url"], "https://example.com/aerial.tif")
        self.assertEqual(metadata["local_path"], "data/aerial.tif")


if __name__ == "__main__":
    unittest.main()
