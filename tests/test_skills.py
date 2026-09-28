import json
import tempfile
import unittest
from pathlib import Path

from baseline.skills.catalog import SkillCatalog
from baseline.tools.contracts import ToolFailure
from baseline.persistence.store import Store
from smoke_skills import SCENARIOS, ROOT, run_scenario


class SkillTests(unittest.TestCase):
    def test_catalog_is_metadata_only_and_load_is_bounded(self):
        catalog = SkillCatalog(ROOT / "skills")
        self.assertNotIn("执行流程", catalog.prompt())
        result = catalog.tool().handler({"name": "code-documentation"}, None)
        self.assertIn("执行流程", result.content["instructions"])
        self.assertEqual(len(result.content["sha256"]), 64)

    def test_unknown_name_cannot_be_a_path(self):
        catalog = SkillCatalog(ROOT / "skills")
        for name in ("../.env", "code-documentation/upstream", "unknown"):
            with self.assertRaises(ToolFailure):
                catalog.path(name)

    def test_missing_and_oversized_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "catalog.json").write_text(json.dumps([{"name": "one", "description": "test"}]))
            with self.assertRaises(ToolFailure):
                SkillCatalog(root)
            (root / "one").mkdir()
            (root / "one" / "SKILL.md").write_text("x" * 16001)
            with self.assertRaises(ToolFailure):
                SkillCatalog(root).tool().handler({"name": "one"}, None)

    def test_both_skills_end_to_end_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "work"
            store = Store(Path(directory) / "state")
            for scenario in SCENARIOS:
                report = run_scenario(scenario, live=False, base=base, skills=SkillCatalog(ROOT / "skills"), store=store, verbose=False)
                self.assertTrue(report["passed"], report)
