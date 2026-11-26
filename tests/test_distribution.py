"""Verify the installable skill and source-only plugin archive in isolated directories."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("KORO_TEST_NODE") or shutil.which("node")


@unittest.skipUnless(NODE, "Node >=22.15 is required for repository installation and bundling")
class DistributionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="koro-distribution-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {**os.environ, "CODEX_HOME": str(self.root / "codex")}

    def node(self, script, *args, cwd=ROOT):
        result = subprocess.run([NODE, str(script), *map(str, args)], cwd=cwd, env=self.env,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_operations_only_install_needs_no_hypit_and_preserves_other_skills(self):
        existing = self.root / "codex/skills/video-director/SKILL.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("user's existing director", encoding="utf-8")
        self.node(ROOT / "scripts/workflow.mjs", "install-skill", "--operations-only")
        skill = self.root / "codex/skills/koro-operations"
        index = json.loads((skill / "references/local-setup.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(index["operationsScript"]), skill / "scripts/operations.py")
        self.assertEqual(existing.read_text(encoding="utf-8"), "user's existing director")
        self.assertFalse(list(skill.rglob("__pycache__")))
        config = self.root / "local/config.json"
        result = subprocess.run([sys.executable, "-X", "utf8", str(skill / "scripts/operations.py"), "init",
                                 "--config", str(config), "--workspace", str(self.root / "workspace")],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["format"], "koro.operations@1")

    def test_core_only_install_is_self_contained_and_preserves_other_skills(self):
        self.node(ROOT / "scripts/workflow.mjs", "install-skill", "--core-only")
        skill = self.root / "codex/skills/koroflow"
        workspace = self.root / "workspace"
        call = subprocess.run([sys.executable, "-X", "utf8", str(skill / "scripts/workflow.py"),
            "--workspace", str(workspace), "init"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertTrue(json.loads(call.stdout)["initialized"])
        self.assertFalse((self.root / "codex/skills/hypit").exists())

    def test_skill_upgrade_preserves_the_selected_workspace_and_operations_index(self):
        custom_config = str(self.root / "private" / "operations.json")
        for skill_name, option, overrides in (
            ("koroflow", "--core-only", {"operationsConfig": custom_config, "workspace": str(self.root / "active")}),
            ("koro-operations", "--operations-only", {"configFile": custom_config}),
        ):
            self.node(ROOT / "scripts/workflow.mjs", "install-skill", option)
            path = self.root / "codex/skills" / skill_name / "references/local-setup.json"
            settings = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(json.dumps({**settings, **overrides}), encoding="utf-8")
            self.node(ROOT / "scripts/workflow.mjs", "install-skill", option)
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({key: updated[key] for key in overrides}, overrides)

    def test_source_zip_contains_plugin_and_runs_after_extraction(self):
        archive = self.root / "bundle.zip"
        self.node(ROOT / "scripts/bundle.mjs", archive)
        with zipfile.ZipFile(archive) as z:
            self.assertIsNone(z.testzip())
            names = z.namelist()
            self.assertTrue(any(n.endswith("/plugin.json") for n in names))
            self.assertTrue(any(n.endswith("/.agents/plugins/marketplace.json") for n in names))
            for name in names:
                parts = Path(name).parts
                self.assertFalse({".local", "node_modules", "__pycache__", ".git", ".hypit", ".koroflow"}.intersection(parts), name)
                self.assertFalse(name.endswith(("local-setup.json", ".pyc", ".mp4", ".wav", "tokens.json")), name)
            z.extractall(self.root / "unpacked")
        extracted = self.root / "unpacked/hypit-haiyi-workflow"
        self.assertTrue((extracted / "LICENSE").is_file())
        self.assertTrue((extracted / "assets/koroflow-logo.svg").is_file())
        for skill_file in (ROOT / "skills").rglob("*"):
            if not skill_file.is_file():
                continue
            rel = skill_file.relative_to(ROOT)
            if "__pycache__" in rel.parts or rel.name == "local-setup.json" or rel.suffix == ".pyc":
                continue
            self.assertTrue((extracted / rel).is_file(), f"Missing skill file in bundle: {rel}")
        manifest = json.loads((extracted / "plugin.json").read_text(encoding="utf-8"))
        package = json.loads((extracted / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], package["version"])
        catalog = json.loads((extracted / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        entry = catalog["plugins"][0]
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual((extracted / entry["source"]["path"]).resolve(), extracted.resolve())
        self.node(extracted / "scripts/workflow.mjs", "install-skill", "--operations-only", cwd=extracted)
        result = subprocess.run([sys.executable, "-X", "utf8", str(extracted / "scripts/operations.py"), "--help"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plan", result.stdout)
        self.node(extracted / "scripts/workflow.mjs", "install-skill", "--core-only", cwd=extracted)
        call = subprocess.run([sys.executable, "-X", "utf8", str(extracted / "scripts/koroflow.py"),
            "--workspace", str(self.root / "workspace"), "init"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertTrue(json.loads(call.stdout)["initialized"])

    def test_markdown_relative_links_resolve(self):
        md_files = [
            ROOT / "README.md",
            ROOT / "AGENTS.md",
            ROOT / "NOTICE.md",
            ROOT / "CHANGELOG.md",
            *(ROOT / "docs").rglob("*.md"),
            *(ROOT / "skills").rglob("*.md"),
        ]
        fenced_re = re.compile(r"```[\s\S]*?```", re.MULTILINE)
        inline_re = re.compile(r"`[^`]*`")
        link_re = re.compile(r"(?<!!)\[[^\]]+\]\((<[^>]+>|[^)\s]+)\)")
        broken = []
        for md in md_files:
            text = md.read_text(encoding="utf-8")
            cleaned = inline_re.sub("", fenced_re.sub("", text))
            for raw_target in link_re.findall(cleaned):
                target = raw_target[1:-1] if raw_target.startswith("<") and raw_target.endswith(">") else raw_target
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path_part = target.split("#", 1)[0]
                if not path_part:
                    continue
                resolved = (md.parent / path_part).resolve()
                if not resolved.exists():
                    broken.append(f"{md.relative_to(ROOT)} -> {raw_target}")
        self.assertEqual(broken, [], "Broken relative links:\n" + "\n".join(broken))


if __name__ == "__main__":
    unittest.main()
