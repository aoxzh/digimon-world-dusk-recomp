"""ROM-free checks for the repository. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import py_compile
import re
import subprocess
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROM_SHA1 = "de1a1915e07288ffad654f3f9f2eb730a0fd1d9c"


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], check=True,
                             capture_output=True).stdout.decode("utf-8")
        files = [REPO / p for p in out.split("\0") if p]
        if files:
            return [p for p in files if p.is_file()]
    except (OSError, subprocess.CalledProcessError):
        pass
    skip = {".git", "external", "generated", "build", "run", "dist", ".venv", "__pycache__"}
    return [p for p in REPO.rglob("*") if p.is_file() and not skip & set(p.relative_to(REPO).parts)]


class ConfigTests(unittest.TestCase):
    def test_game_toml_identity(self):
        game = tomllib.loads((REPO / "game.toml").read_text(encoding="utf-8"))["game"]
        self.assertEqual(game["id"], "A6RE")
        self.assertEqual(game["sha1"], ROM_SHA1)
        self.assertEqual(game["rom_size"], 0x04000000)

    def test_tools_agree_on_rom_identity(self):
        for tool in ("tools/build_dusk.sh", "tools/prepare_dusk.py", "tools/promote_coverage.py"):
            self.assertIn(ROM_SHA1, (REPO / tool).read_text(encoding="utf-8"), tool)

    def test_framework_lock(self):
        fw = json.loads((REPO / "framework.lock.json").read_text(encoding="utf-8"))
        for name in ("ndsrecomp", "recomp-ui"):
            self.assertTrue(fw[name]["url"].startswith("https://"), name)
            self.assertRegex(fw[name]["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue((REPO / fw["ndsrecomp"]["patch"]).is_file())

    def test_python_tools_compile(self):
        for path in (REPO / "tools").glob("*.py"):
            py_compile.compile(str(path), doraise=True)


class PrivacyTests(unittest.TestCase):
    """Nothing private or game-derived may be committed."""

    def test_no_game_or_console_data(self):
        for path in tracked_files():
            name = path.name.lower()
            self.assertFalse(name.endswith((".nds", ".sav", ".dsv", ".srl")), path)
            self.assertFalse(re.match(r"bios(nds|dsi)?[79]?.*\.(bin|rom)$", name), path)
            self.assertFalse("firmware" in name and name.endswith(".bin"), path)
            self.assertLess(path.stat().st_size, 4_000_000, f"unexpectedly large file: {path}")

    def test_no_personal_paths(self):
        patterns = [re.compile(p, re.I) for p in (
            r"[a-z]:[\\/]+users[\\/]+(?!<|\.\.\.)[a-z0-9._-]+",  # C:\Users\<someone>
            r"/(?:home|Users)/(?!runner\b)[a-z0-9._-]+/",        # /home/<someone>/, /Users/<someone>/
            r"AppData[\\/]+Local[\\/]+Temp",
            r"/sd" r"card/|/data/" r"local/",                     # device paths
        )]
        for path in tracked_files():
            if path.suffix.lower() in (".png", ".jpg", ".ico", ".patch"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in patterns:
                match = pattern.search(text)
                self.assertIsNone(match, f"{path.relative_to(REPO)}: {match and match.group(0)}")


if __name__ == "__main__":
    unittest.main()
