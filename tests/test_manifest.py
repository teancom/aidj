"""Lightweight repository checks that do not require a Home Assistant install."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class ManifestTests(unittest.TestCase):
    """Validate the files needed for a HACS integration."""

    def test_manifest_has_required_metadata(self) -> None:
        manifest = json.loads(
            (ROOT / "custom_components" / "aidj" / "manifest.json").read_text()
        )
        for key in (
            "domain",
            "name",
            "codeowners",
            "config_flow",
            "documentation",
            "integration_type",
            "issue_tracker",
            "version",
        ):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["domain"], "aidj")
        self.assertTrue(manifest["config_flow"])

    def test_hacs_metadata_has_name(self) -> None:
        hacs = json.loads((ROOT / "hacs.json").read_text())
        self.assertEqual(hacs["name"], "AI DJ")


if __name__ == "__main__":
    unittest.main()
