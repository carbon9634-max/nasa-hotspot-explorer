"""The Cloud Run image must include every local module app.py imports."""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _imported_local_modules():
    tree = ast.parse((ROOT / "app.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return {name for name in names if (ROOT / f"{name}.py").exists()}


class DockerfileTests(unittest.TestCase):
    def test_dockerfile_copies_imported_modules(self):
        modules = _imported_local_modules()
        self.assertIn("archive", modules)
        self.assertIn("harmonize", modules)
        copy_text = "\n".join(
            line
            for line in (ROOT / "Dockerfile").read_text().splitlines()
            if line.strip().startswith("COPY")
        )
        self.assertIn("app_data.csv", copy_text)
        if "*.py" in copy_text:
            pass
        else:
            missing = [name for name in modules if f"{name}.py" not in copy_text]
            self.assertFalse(missing, f"Dockerfile COPY is missing {missing}")
        for name in (".dockerignore", ".gcloudignore"):
            ignored = (ROOT / name).read_text()
            self.assertIn("ingest/", ignored, f"{name} must keep the ingest job out of the app image")


if __name__ == "__main__":
    unittest.main()
