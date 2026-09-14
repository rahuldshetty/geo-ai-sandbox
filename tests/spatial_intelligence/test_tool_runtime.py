"""Tool runtime: binding, artifact recording, notifications, and approval."""

import tempfile
import unittest

from spatial_intelligence.contracts.errors import RuntimeNotBoundError, ToolInputError
from spatial_intelligence.tools import ToolRuntime, bind, current_runtime, maybe_runtime
from spatial_intelligence.tools.runtime import RuntimeEvents
from spatial_intelligence.workspace import Workspace


class RuntimeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(self._tmp.name).create()
        self.runtime = ToolRuntime(workspace=self.workspace, run_id="run-1")

    def tearDown(self):
        self._tmp.cleanup()


class BindingTests(RuntimeTestCase):
    def test_binding_is_scoped_and_nesting_restores_the_outer_runtime(self):
        outer = self.runtime
        inner = self.runtime.evolve(run_id="run-2")

        self.assertIsNone(maybe_runtime())
        with bind(outer):
            self.assertIs(current_runtime(), outer)
            with bind(inner):
                self.assertIs(current_runtime(), inner)
            self.assertIs(current_runtime(), outer)
        self.assertIsNone(maybe_runtime())

    def test_unbound_access_raises_a_specific_error(self):
        self.assertIsNone(maybe_runtime())

        with self.assertRaises(RuntimeNotBoundError):
            current_runtime()

    def test_binding_a_second_session_does_not_leak(self):
        other = self.runtime.evolve(run_id="other")

        with bind(self.runtime):
            pass
        with bind(other):
            self.assertEqual(current_runtime().run_id, "other")


class ArtifactTests(RuntimeTestCase):
    def test_record_artifact_updates_the_manifest_and_notifies(self):
        notified: list[int] = []
        runtime = self.runtime.evolve(
            events=RuntimeEvents(files_changed=lambda: notified.append(1))
        )
        target = self.workspace.results / "out.txt"
        target.write_text("x", encoding="utf-8")

        absolute = runtime.record_artifact(target)

        self.assertEqual(absolute, str(target))
        self.assertEqual(notified, [1])
        manifest = self.workspace._read_manifest()
        self.assertIn("results/out.txt", manifest["outputs"])

    def test_notification_failures_never_reach_the_tool(self):
        def explode() -> None:
            raise RuntimeError("subscriber went away")

        runtime = self.runtime.evolve(
            events=RuntimeEvents(files_changed=explode, map_changed=explode)
        )

        runtime.events.notify_files()  # must not raise
        runtime.events.notify_map()

    def test_missing_hooks_are_no_ops(self):
        self.runtime.events.notify_files()
        self.runtime.events.notify_map()


class ApprovalTests(RuntimeTestCase):
    def test_approval_is_required_until_granted(self):
        with self.assertRaises(ToolInputError) as caught:
            self.runtime.require_approval("band_math", hint="Use run_python instead.")

        self.assertIn("band_math", str(caught.exception))
        self.assertIn("Use run_python instead.", str(caught.exception))

    def test_granted_approval_allows_the_call(self):
        approved = self.runtime.evolve(approved=True)

        approved.require_approval("band_math")


class MapAndUrlTests(RuntimeTestCase):
    def test_map_access_raises_when_the_session_has_none(self):
        with self.assertRaises(RuntimeNotBoundError):
            self.runtime.require_map()

    def test_map_access_returns_the_bound_map(self):
        sentinel = object()
        runtime = self.runtime.evolve(map=sentinel)

        self.assertIs(runtime.require_map(), sentinel)

    def test_file_url_normalizes_workspace_paths(self):
        runtime = self.runtime.evolve(base_url="http://127.0.0.1:8010/")

        self.assertEqual(
            runtime.file_url("data\\scene.tif"),
            "http://127.0.0.1:8010/api/files/data/scene.tif",
        )
        self.assertEqual(
            runtime.file_url("/results/a b.tif"),
            "http://127.0.0.1:8010/api/files/results/a b.tif",
        )


if __name__ == "__main__":
    unittest.main()
