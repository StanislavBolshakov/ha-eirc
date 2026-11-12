"""Check the repository layout required for installation through HACS."""

import json
import unittest
from pathlib import Path


class PackagingTests(unittest.TestCase):
    def test_root_hacs_manifest_and_integration_layout(self):
        root = Path(__file__).resolve().parents[1]
        hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
        self.assertIsInstance(hacs.get("name"), str)
        self.assertTrue(hacs["name"].strip())
        self.assertFalse(hacs.get("content_in_root", False))
        self.assertFalse(hacs.get("zip_release", False))
        manifests = list((root / "custom_components").glob("*/manifest.json"))
        self.assertEqual(manifests, [root / "custom_components/eirc/manifest.json"])

    def test_integration_manifest_has_hacs_required_fields(self):
        path = (
            Path(__file__).resolve().parents[1] / "custom_components/eirc/manifest.json"
        )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for field in (
            "domain",
            "documentation",
            "issue_tracker",
            "codeowners",
            "name",
            "version",
        ):
            with self.subTest(field=field):
                self.assertTrue(manifest.get(field))
        self.assertEqual(manifest["domain"], "eirc")
