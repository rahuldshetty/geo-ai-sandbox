"""Raster service and pack: inspection, stretching, warping, and approval."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin

from spatial_intelligence.contracts.effects import Effect
from spatial_intelligence.contracts.errors import ToolInputError, WorkspaceError
from spatial_intelligence.contracts.progress import JobState, Reporter
from spatial_intelligence.geo import raster
from spatial_intelligence.tools import ToolRegistry, ToolRuntime
from spatial_intelligence.tools.packs.raster import RasterPack
from spatial_intelligence.tools.runtime import RuntimeEvents
from spatial_intelligence.workspace import Workspace

SRC = "data/scene.tif"


class RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event.as_dict())

    def for_job(self, job_id):
        return [event for event in self.events if event["job_id"] == job_id]

    def statuses(self):
        return [event["status"] for event in self.events]


class RasterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self._tmp.name) / "workspace").create()

    def tearDown(self):
        self._tmp.cleanup()

    def write_raster(self, values=None, *, count=1, dtype="float32", nodata=None):
        """Write a 4x3 synthetic raster into ``data/`` and return its path."""
        if values is None:
            values = np.arange(1, 3 * 4 * count + 1, dtype=dtype).reshape(count, 3, 4)
        profile = {
            "driver": "GTiff",
            "width": 4,
            "height": 3,
            "count": count,
            "dtype": dtype,
            "crs": "EPSG:4326",
            "transform": from_origin(0.0, 1.0, 0.25, 0.25),
        }
        if nodata is not None:
            profile["nodata"] = nodata
        path = self.workspace.resolve(SRC, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(path, "w", **profile) as ds:
            ds.write(np.asarray(values, dtype=dtype))
        return path

    def write_mask(self, geometry: dict, relative: str = "data/mask.geojson") -> str:
        path = self.workspace.resolve(relative, write=True)
        path.write_text(
            json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature",
                        "properties": {}, "geometry": geometry}]}),
            encoding="utf-8",
        )
        return relative

    def manifest_outputs(self) -> list[str]:
        manifest = json.loads(self.workspace.manifest_path.read_text(encoding="utf-8"))
        return manifest["outputs"]


class RasterInfoTests(RasterTestCase):
    def test_raster_info_reports_crs_size_bands_and_bounds(self):
        self.write_raster(count=2, dtype="uint16")

        info = raster.raster_info(self.workspace, SRC)

        self.assertEqual(info["crs"], "EPSG:4326")
        self.assertEqual((info["width"], info["height"]), (4, 3))
        self.assertEqual(info["count"], 2)
        self.assertEqual(info["dtypes"], ["uint16", "uint16"])
        self.assertEqual(info["bounds"], [0.0, 0.25, 1.0, 1.0])
        self.assertEqual(len(info["transform"]), 6)
        self.assertIsNone(info["nodata"])

    def test_raster_info_reports_a_missing_file(self):
        with self.assertRaises(WorkspaceError) as caught:
            raster.raster_info(self.workspace, "data/missing.tif")

        self.assertIn("missing.tif", str(caught.exception))

    def test_raster_info_refuses_a_path_outside_the_workspace(self):
        with self.assertRaises(WorkspaceError):
            raster.raster_info(self.workspace, "../outside.tif")


class RasterStatsTests(RasterTestCase):
    def test_stats_return_a_summary_and_a_256_bin_histogram(self):
        self.write_raster(values=np.arange(12, dtype="float32").reshape(1, 3, 4))

        stats = raster.raster_stats(self.workspace, SRC)

        self.assertEqual(stats["min"], 0.0)
        self.assertEqual(stats["max"], 11.0)
        self.assertEqual(stats["mean"], 5.5)
        self.assertEqual(len(stats["histogram"]), 256)
        self.assertEqual(sum(stats["histogram"]), 12)
        self.assertLess(stats["p2"], stats["p98"])
        self.assertEqual(stats["p2"], float(np.nanpercentile(np.arange(12), 2)))

    def test_stats_return_an_empty_summary_for_a_fully_masked_band(self):
        self.write_raster(
            values=np.full((1, 3, 4), -9999.0, dtype="float32"), nodata=-9999.0
        )

        stats = raster.raster_stats(self.workspace, SRC)

        self.assertIsNone(stats["min"])
        self.assertIsNone(stats["p98"])
        self.assertEqual(stats["histogram"], [])

    def test_sample_point_reads_the_pixel_value(self):
        self.write_raster(values=np.arange(12, dtype="float32").reshape(1, 3, 4))

        sampled = raster.sample_point(self.workspace, SRC, 0.375, 0.875)

        self.assertEqual(sampled, {"value": 1.0, "lng": 0.375, "lat": 0.875})


class RasterWriterTests(RasterTestCase):
    def test_to_cog_writes_a_readable_copy(self):
        self.write_raster(count=2)

        written = raster.to_cog(self.workspace, SRC, "results/scene_cog.tif")

        self.assertEqual(written, self.workspace.results / "scene_cog.tif")
        with rasterio.open(written) as ds:
            self.assertEqual((ds.count, ds.width, ds.height), (2, 4, 3))
            self.assertEqual(ds.crs.to_epsg(), 4326)

    def test_reproject_writes_the_destination_crs(self):
        self.write_raster()

        written = raster.reproject(self.workspace, SRC, "results/3857.tif", "EPSG:3857")

        with rasterio.open(written) as ds:
            self.assertEqual(ds.crs.to_epsg(), 3857)
            self.assertEqual(ds.count, 1)

    def test_clip_writes_only_the_requested_window(self):
        self.write_raster()

        written = raster.clip(
            self.workspace, SRC, "results/clip.tif", bounds=[0.0, 0.25, 0.5, 0.75]
        )

        with rasterio.open(written) as ds:
            self.assertEqual((ds.width, ds.height), (2, 2))
            self.assertEqual(list(ds.bounds), [0.0, 0.25, 0.5, 0.75])
            np.testing.assert_allclose(ds.read(1), [[5.0, 6.0], [9.0, 10.0]])

    def test_clip_masks_to_a_geojson_feature_collection(self):
        self.write_raster()
        mask_path = self.write_mask(
            {
                "type": "Polygon",
                "coordinates": [
                    [[0.0, 0.25], [0.5, 0.25], [0.5, 0.75], [0.0, 0.75], [0.0, 0.25]]
                ],
            }
        )

        written = raster.clip(self.workspace, SRC, "results/masked.tif", mask_geojson=mask_path)

        with rasterio.open(written) as ds:
            self.assertEqual((ds.width, ds.height), (2, 2))
            np.testing.assert_allclose(ds.read(1), [[5.0, 6.0], [9.0, 10.0]])

    def test_clip_without_bounds_or_mask_fails(self):
        self.write_raster()

        with self.assertRaises(WorkspaceError) as caught:
            raster.clip(self.workspace, SRC, "results/clip.tif")

        self.assertIn("bounds or mask_geojson", str(caught.exception))

    def test_rescale_stretches_the_band_to_uint8(self):
        self.write_raster(values=np.arange(12, dtype="float32").reshape(1, 3, 4))

        written = raster.rescale(self.workspace, SRC, "results/stretch.tif")

        with rasterio.open(written) as ds:
            self.assertEqual(ds.dtypes, ("uint8",))
            self.assertEqual(ds.count, 1)
            data = ds.read(1)
        self.assertEqual((int(data.min()), int(data.max())), (0, 255))

    def test_rescale_honors_an_explicit_range(self):
        self.write_raster(values=np.arange(12, dtype="float32").reshape(1, 3, 4))

        written = raster.rescale(self.workspace, SRC, "results/+stretch.tif", vmin=0.0, vmax=11.0)

        with rasterio.open(written) as ds:
            data = ds.read(1)
        self.assertEqual((int(data[0, 0]), int(data[2, 3])), (0, 255))

    def test_rescale_maps_source_nodata_to_zero_when_asked(self):
        values = np.arange(12, dtype="float32").reshape(1, 3, 4).copy()
        values[0, 2, 3] = -9999.0
        self.write_raster(values=values, nodata=-9999.0)

        written = raster.rescale(self.workspace, SRC, "results/masked.tif", nodata=0.0)

        with rasterio.open(written) as ds:
            self.assertEqual(ds.nodata, 0.0)
            self.assertEqual(int(ds.read(1)[2, 3]), 0)

    def test_band_math_evaluates_the_expression(self):
        values = np.arange(24, dtype="float32").reshape(2, 3, 4)
        self.write_raster(values=values, count=2)

        written = raster.band_math(
            self.workspace, SRC, "results/index.tif", "(b2 - b1) / 2", {"b1": 1, "b2": 2}
        )

        with rasterio.open(written) as ds:
            self.assertEqual(ds.count, 1)
            np.testing.assert_allclose(ds.read(1), np.full((3, 4), 6.0))

    def test_gdal_translate_explains_itself_without_osgeo(self):
        self.write_raster()

        with patch.dict(sys.modules, {"osgeo": None}):
            result = raster.gdal_translate(
                self.workspace, SRC, "results/translated.tif"
            )

        self.assertIsInstance(result, str)
        self.assertIn("gdal_translate unavailable", result)
        self.assertIn("rasterio", result)
        self.assertFalse((self.workspace.results / "translated.tif").exists())


class RasterPackTests(RasterTestCase):
    def build_pack(self, sink=None, notifications=None, approved=False):
        runtime = ToolRuntime(
            workspace=self.workspace,
            reporter=Reporter(sink or RecordingSink(), parent_id="cell-7"),
            events=RuntimeEvents(
                files_changed=lambda: (notifications if notifications is not None else []).append(
                    "files"
                )
            ),
            approved=approved,
        )
        registry = ToolRegistry()
        registry.add_pack(RasterPack, runtime)
        return registry

    def done_events(self, sink):
        return [event for event in sink.events if event["status"] == JobState.DONE.value]

    def assert_recorded(self, relative: str) -> Path:
        path = self.workspace.resolve(relative)
        self.assertTrue(path.is_file(), f"{relative} was not written")
        self.assertIn(relative, self.manifest_outputs())
        return path

    def test_registered_raster_tools_expose_expected_effects(self):
        registry = self.build_pack()

        self.assertEqual(
            sorted(registry.names()),
            [
                "band_math",
                "clip",
                "gdal_translate",
                "raster_info",
                "raster_stats",
                "reproject",
                "rescale",
                "sample_point",
                "to_cog",
            ],
        )
        for name in ("raster_info", "raster_stats", "sample_point"):
            spec = registry.get(name)
            self.assertEqual(spec.effects, frozenset({Effect.READ}), name)
            self.assertEqual(spec.category, "raster")
            self.assertTrue(registry.replay_safe(name), name)
        for name in ("to_cog", "reproject", "clip", "rescale", "band_math", "gdal_translate"):
            spec = registry.get(name)
            self.assertEqual(spec.effects, frozenset({Effect.WORKSPACE_WRITE}), name)
            self.assertFalse(registry.replay_safe(name), name)
        for name in ("to_cog", "reproject", "clip", "rescale"):
            self.assertEqual(registry.get(name).kind.value, "reporting", name)
        self.assertTrue(registry.get("band_math").requires_approval)
        self.assertFalse(registry.get("to_cog").requires_approval)
        self.assertEqual(registry.category_of("rescale"), "raster")

    def test_to_cog_reports_band_progress_and_records_the_artifact(self):
        self.write_raster(count=2)
        sink = RecordingSink()
        notifications: list[str] = []
        registry = self.build_pack(sink, notifications)

        absolute = registry.get("to_cog").callable(SRC, "results/scene_cog.tif")

        written = self.assert_recorded("results/scene_cog.tif")
        self.assertEqual(Path(absolute), written)
        self.assertEqual(notifications, ["files"])
        with rasterio.open(written) as ds:
            self.assertEqual(ds.count, 2)

        events = sink.events
        self.assertEqual({event["kind"] for event in events}, {"raster"})
        self.assertEqual(events[0]["label"], "to_cog results/scene_cog.tif")
        self.assertEqual(events[0]["unit"], "steps")
        self.assertEqual(events[0]["total"], 2)
        self.assertEqual(events[0]["parent_id"], "cell-7")
        # One report per band, then the terminal event carrying the artifact.
        self.assertEqual(
            [event["completed"] for event in events if event["status"] == "running"],
            [0.0, 1.0, 2.0],
        )
        final = events[-1]
        self.assertEqual(final["status"], JobState.DONE.value)
        self.assertEqual(final["artifact"], "results/scene_cog.tif")
        self.assertEqual(final["completed"], 2.0)

    def test_reproject_reports_a_done_job_with_the_artifact(self):
        self.write_raster(count=2)
        sink = RecordingSink()
        registry = self.build_pack(sink)

        registry.get("reproject").callable(SRC, "results/scene_3857.tif", "EPSG:3857")

        written = self.assert_recorded("results/scene_3857.tif")
        with rasterio.open(written) as ds:
            self.assertEqual(ds.crs.to_epsg(), 3857)
        done = self.done_events(sink)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["artifact"], "results/scene_3857.tif")
        self.assertEqual(done[0]["total"], 2)

    def test_clip_reports_a_done_job_with_the_artifact(self):
        self.write_raster()
        sink = RecordingSink()
        registry = self.build_pack(sink)

        registry.get("clip").callable(SRC, "results/clip.tif", [0.0, 0.25, 0.5, 0.75])

        written = self.assert_recorded("results/clip.tif")
        with rasterio.open(written) as ds:
            self.assertEqual((ds.width, ds.height), (2, 2))
        done = self.done_events(sink)
        self.assertEqual([event["artifact"] for event in done], ["results/clip.tif"])
        self.assertEqual(done[0]["total"], 1)

    def test_clip_without_bounds_or_mask_fails_the_job(self):
        self.write_raster()
        sink = RecordingSink()
        registry = self.build_pack(sink)

        with self.assertRaises(WorkspaceError):
            registry.get("clip").callable(SRC, "results/clip.tif")

        self.assertEqual(sink.statuses()[-1], JobState.ERROR.value)
        self.assertIn("bounds or mask_geojson", sink.events[-1]["error"])

    def test_rescale_reports_a_done_job_with_the_artifact(self):
        self.write_raster(values=np.arange(12, dtype="float32").reshape(1, 3, 4))
        sink = RecordingSink()
        registry = self.build_pack(sink)

        registry.get("rescale").callable(SRC, "results/stretch.tif")

        written = self.assert_recorded("results/stretch.tif")
        with rasterio.open(written) as ds:
            self.assertEqual(ds.dtypes, ("uint8",))
        done = self.done_events(sink)
        self.assertEqual([event["artifact"] for event in done], ["results/stretch.tif"])

    def test_missing_source_fails_the_rescale_job(self):
        sink = RecordingSink()
        registry = self.build_pack(sink)

        with self.assertRaises(WorkspaceError):
            registry.get("rescale").callable("data/missing.tif", "results/stretch.tif")

        self.assertEqual(sink.statuses()[-1], JobState.ERROR.value)
        self.assertIn("missing.tif", sink.events[-1]["error"])

    def test_band_math_refuses_to_run_without_approval(self):
        self.write_raster(count=2)
        registry = self.build_pack()

        with self.assertRaises(ToolInputError) as caught:
            registry.get("band_math").callable(
                SRC, "results/index.tif", "b2 - b1", {"b1": 1, "b2": 2}
            )

        self.assertIn("band_math", str(caught.exception))
        self.assertIn("run_python", str(caught.exception))
        self.assertFalse((self.workspace.results / "index.tif").exists())
        self.assertEqual(self.manifest_outputs(), [])

    def test_band_math_computes_the_index_once_approved(self):
        values = np.arange(24, dtype="float32").reshape(2, 3, 4)
        self.write_raster(values=values, count=2)
        registry = self.build_pack(approved=True)

        absolute = registry.get("band_math").callable(
            SRC, "results/ndvi.tif", "(b2 - b1) / (b2 + b1)", {"b1": 1, "b2": 2}
        )

        self.assert_recorded("results/ndvi.tif")
        with rasterio.open(absolute) as ds:
            bands = values.astype("float64")
            expected = (bands[1] - bands[0]) / (bands[1] + bands[0])
            np.testing.assert_allclose(ds.read(1), expected)

    def test_sample_point_returns_the_value_without_recording_anything(self):
        self.write_raster(values=np.arange(12, dtype="float32").reshape(1, 3, 4))
        notifications: list[str] = []
        registry = self.build_pack(notifications=notifications)

        sampled = registry.get("sample_point").callable(SRC, 0.375, 0.875)

        self.assertEqual(sampled, {"value": 1.0, "lng": 0.375, "lat": 0.875})
        self.assertEqual(notifications, [])
        self.assertEqual(self.manifest_outputs(), [])

    def test_gdal_translate_returns_the_message_without_recording(self):
        self.write_raster()
        registry = self.build_pack()

        with patch.dict(sys.modules, {"osgeo": None}):
            result = registry.get("gdal_translate").callable(SRC, "results/translated.tif")

        self.assertIn("gdal_translate unavailable", result)
        self.assertFalse((self.workspace.results / "translated.tif").exists())
        self.assertEqual(self.manifest_outputs(), [])


if __name__ == "__main__":
    unittest.main()
