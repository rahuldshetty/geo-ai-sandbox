"""The registry manifest: complete, classified, and free of drift.

This is where the three hand-maintained name lists of the previous package are
replaced by one manifest, so these tests are the guard that the replacement
stays complete: every tool classified, every capability's tools real, and the
externally-registered names still matching the installed libraries.
"""

import tempfile
import unittest
from pathlib import Path

from spatial_intelligence import discovery
from spatial_intelligence.agent.builder import build_agent
from spatial_intelligence.contracts.effects import Effect
from spatial_intelligence.tools.build import (
    PLAN_MUTATION_NAMES,
    PLAN_TOOL_NAMES,
    TOOL_SEARCH_NAME,
    default_registry,
)
from spatial_intelligence.tools.registry import PLAN_MUTATION_TAG
from spatial_intelligence.tools.runtime import ToolRuntime
from spatial_intelligence.workspace import Workspace

#: Routing, interaction, and orientation tools the model always sees.
EXPECTED_CORE = frozenset(
    {
        "describe_geolibre_bridge",
        "describe_map",
        "discover_capabilities",
        "find_files",
        "list_files",
        "request_user_input",
    }
)

EXPECTED_CATEGORY_COUNTS = {
    "capability": 1,
    "catalog": 5,
    "files": 6,
    "interaction": 1,
    "layers": 21,
    "plan": 6,
    "python": 4,
    "raster": 9,
    "search": 1,
    "vector": 6,
}


class ManifestTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        workspace = Workspace(Path(cls._tmp.name) / "workspace").create()
        cls.runtime = ToolRuntime(workspace=workspace)
        cls.registry = default_registry(cls.runtime)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class ManifestTests(ManifestTestCase):
    def test_every_tool_is_registered_with_a_category(self):
        self.assertEqual(len(self.registry), 60)
        counted = {
            category: len(names)
            for category, names in self.registry.categories().items()
        }
        self.assertEqual(counted, EXPECTED_CATEGORY_COUNTS)
        for spec in self.registry.specs():
            self.assertTrue(spec.category, spec.name)
            self.assertTrue(spec.summary, spec.name)

    def test_core_tools_are_the_routing_set(self):
        self.assertEqual(self.registry.core_names(), EXPECTED_CORE)

    def test_every_registered_tool_carries_effects(self):
        for spec in self.registry.specs():
            self.assertTrue(spec.effects, spec.name)
            self.assertIsInstance(spec.effects, frozenset)

    def test_mutating_tools_are_not_replay_safe(self):
        for spec in self.registry.specs():
            expected = not (spec.effects & {Effect.WORKSPACE_WRITE, Effect.MAP_WRITE})
            self.assertEqual(spec.replay_safe, expected, spec.name)
        self.assertFalse(self.registry.replay_safe("add_raster"))
        self.assertTrue(self.registry.replay_safe("raster_info"))
        self.assertFalse(self.registry.replay_safe("brand_new_tool"))

    def test_plan_tools_are_classified_and_replay_safe(self):
        for name in PLAN_TOOL_NAMES:
            spec = self.registry.get(name)
            self.assertEqual(spec.category, "plan")
            self.assertEqual(spec.origin, "harness")
            self.assertTrue(spec.replay_safe, name)
            self.assertEqual(
                self.registry.plan_mutation(name), name in PLAN_MUTATION_NAMES
            )

    def test_tool_search_is_classified_read_only(self):
        spec = self.registry.get(TOOL_SEARCH_NAME)
        self.assertEqual(spec.origin, "toolsearch")
        self.assertEqual(spec.effects, frozenset({Effect.READ}))
        self.assertFalse(spec.implemented)

    def test_implemented_tools_exclude_foreign_ones(self):
        implemented = {spec.name for spec in self.registry.implemented()}
        self.assertEqual(len(implemented), 53)
        self.assertNotIn(TOOL_SEARCH_NAME, implemented)
        for name in PLAN_TOOL_NAMES:
            self.assertNotIn(name, implemented)

    def test_the_handoff_tools_are_declared(self):
        self.assertEqual(
            [spec.name for spec in self.registry.interactive()], ["request_user_input"]
        )

    def test_only_band_math_requires_approval(self):
        requiring = {
            spec.name for spec in self.registry.specs() if spec.requires_approval
        }
        self.assertEqual(requiring, {"band_math"})

    def test_reporting_tools_are_declared_as_such(self):
        reporting = {spec.name for spec in self.registry.specs() if spec.kind.value == "reporting"}
        self.assertTrue(
            {
                "download",
                "download_files",
                "download_catalog_scene",
                "to_cog",
                "reproject",
                "clip",
                "rescale",
            }.issubset(reporting),
            reporting,
        )


class DriftTests(ManifestTestCase):
    def test_plan_tool_names_match_the_installed_harness(self):
        from pydantic_ai_harness.planning._toolset import CORE_TOOL_NAMES

        self.assertEqual(set(PLAN_TOOL_NAMES), set(CORE_TOOL_NAMES))
        self.assertTrue(PLAN_MUTATION_NAMES.issubset(set(CORE_TOOL_NAMES)))

    def test_tool_search_name_matches_the_installed_toolset(self):
        from pydantic_ai.toolsets._tool_search import TOOL_SEARCH_FUNCTION_TOOL_NAME

        self.assertEqual(TOOL_SEARCH_NAME, TOOL_SEARCH_FUNCTION_TOOL_NAME)


class DiscoveryTests(ManifestTestCase):
    def test_every_capability_names_registered_tools(self):
        for capability in discovery.CAPABILITIES:
            for name in capability.tools:
                self.assertIn(
                    name,
                    self.registry,
                    f"capability {capability.id} names an unregistered tool",
                )

    def test_the_catalog_still_ranks_and_annotates(self):
        results = discovery.discover(self.registry, "flood imagery download")

        self.assertEqual(results[0]["id"], "catalog.disaster-imagery")
        annotated = [tool for tool in results[0]["tools"] if isinstance(tool, dict)]
        self.assertTrue(annotated)
        for tool in annotated:
            self.assertIn(tool["name"], self.registry)
            self.assertTrue(tool["summary"])


class BuildTests(ManifestTestCase):
    def test_an_agent_can_be_built_from_the_manifest(self):
        built = build_agent(self.registry, "openai:gpt-4o")

        self.assertIsNotNone(built.agent)
        self.assertIsNotNone(built.plan_store)

    def test_the_runtime_carries_the_registry_for_pack_lookups(self):
        from spatial_intelligence.session.services import REGISTRY_SERVICE

        runtime = self.runtime.evolve(services={REGISTRY_SERVICE: self.registry})
        self.assertIs(runtime.services[REGISTRY_SERVICE], self.registry)

    def test_plan_mutation_tag_is_declared_on_the_tools_that_change_the_plan(self):
        tagged = {
            spec.name for spec in self.registry.with_capability(PLAN_MUTATION_TAG)
        }
        self.assertEqual(tagged, set(PLAN_MUTATION_NAMES))


if __name__ == "__main__":
    unittest.main()
