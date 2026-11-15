"""Keep the production image's local Python import closure complete."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _application_import_closure() -> set[str]:
    local_modules = {
        path.stem: path
        for path in PROJECT_ROOT.glob("*.py")
        if " - Copy" not in path.stem
    }
    pending = ["main", "camera_service"]
    visited: set[str] = set()
    while pending:
        module_name = pending.pop()
        if module_name in visited or module_name not in local_modules:
            continue
        visited.add(module_name)
        tree = ast.parse(local_modules[module_name].read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])
        pending.extend(imports.intersection(local_modules) - visited)
    return visited


class ContainerSourceManifestTests(unittest.TestCase):
    def test_docker_image_copies_every_local_admin_and_camera_import(self):
        copied_files: set[str] = set()
        for line in (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("COPY "):
                continue
            tokens = [token for token in line.split()[1:] if not token.startswith("--")]
            copied_files.update(tokens[:-1])

        required = {f"{module}.py" for module in _application_import_closure()}
        missing = sorted(required - copied_files)
        self.assertEqual(missing, [], f"Dockerfile omits imported application modules: {missing}")

    def test_object_detection_manifest_covers_ultralytics_runtime_dependencies(self):
        detection = (PROJECT_ROOT / "requirements-object-detection.txt").read_text(encoding="utf-8")
        base = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("ultralytics-thop>=2.2.3", detection)
        self.assertIn("nvidia-ml-py>=12.0.0", detection)
        self.assertIn('ultralytics-platform>=0.1.45; python_version >= "3.11"', detection)
        self.assertIn("opencv-contrib-python-headless", base)
        self.assertIn('--no-deps "ultralytics==8.4.183"', dockerfile)


if __name__ == "__main__":
    unittest.main()
