import unittest

from geoai.capabilities import discover_capabilities


class CapabilityDiscoveryTests(unittest.TestCase):
    def test_disaster_request_finds_catalog_comparison_and_overture(self):
        found = discover_capabilities(
            "Load Nepal flood before and after imagery and affected buildings",
            limit=8,
        )
        ids = {item["id"] for item in found}

        self.assertIn("catalog.disaster-imagery", ids)
        self.assertIn("map.compare", ids)
        self.assertIn("catalog.overture", ids)

    def test_results_are_bounded(self):
        self.assertEqual(len(discover_capabilities("data map raster", limit=2)), 2)
        self.assertLessEqual(len(discover_capabilities("anything", limit=100)), 8)


if __name__ == "__main__":
    unittest.main()
