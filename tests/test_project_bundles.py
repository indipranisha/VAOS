"""Offline archive validation tests; no VAOS database, models, or services."""

import io
import json
import unittest
import zipfile

from project_bundles import BundleError, build_bundle, inspect_bundle


def sample_project():
    return {
        "name": "Road scene review",
        "description": "A portable configuration for a local camera workflow.",
        "engine_settings": {
            "object_detection_enabled": True,
            "object_tracking_enabled": True,
            "face_matching_enabled": False,
            "object_confidence": 0.4,
            "match_threshold": 0.42,
            "event_cooldown_seconds": 60,
            "object_image_size": 512,
            "camera_frame_interval_ms": 500,
            "camera_max_dimension": 640,
        },
        "dataset_refs": [{"id": "uvh26", "name": "UVH-26", "format": "YOLO detection", "train_count": 120, "val_count": 30, "class_count": 14}],
        "model_refs": [{"id": "best.pt", "name": "Current best.pt", "kind": "custom"}],
    }


class ProjectBundleTests(unittest.TestCase):
    def test_round_trip_contains_only_allowlisted_project_configuration(self):
        archive, manifest = build_bundle(sample_project(), app_version="1.1.0", created_at="2026-11-12T00:00:00Z")
        inspected = inspect_bundle(archive)
        self.assertEqual(inspected["manifest"], manifest)
        self.assertEqual(inspected["project"], sample_project())
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            self.assertEqual(set(source.namelist()), {"manifest.json", "project.json"})
            content = b"".join(source.read(name) for name in source.namelist()).lower()
            self.assertNotIn(b"password", content)
            self.assertNotIn(b"token", content)
            self.assertNotIn(b"profile_photo", content)

    def test_unknown_private_fields_are_rejected(self):
        project = sample_project()
        project["api_token"] = "secret"
        with self.assertRaises(BundleError):
            build_bundle(project, app_version="1.1.0", created_at="now")

    def test_out_of_range_settings_are_rejected(self):
        project = sample_project()
        project["engine_settings"]["object_confidence"] = 1.5
        with self.assertRaisesRegex(BundleError, "between"):
            build_bundle(project, app_version="1.1.0", created_at="now")

    def test_tampered_project_bytes_fail_checksum(self):
        archive, _ = build_bundle(sample_project(), app_version="1.1.0", created_at="now")
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            manifest = json.loads(source.read("manifest.json"))
            project = json.loads(source.read("project.json"))
        project["name"] = "tampered"
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            target.writestr("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")))
            target.writestr("project.json", json.dumps(project, sort_keys=True, separators=(",", ":")))
        with self.assertRaisesRegex(BundleError, "checksum"):
            inspect_bundle(output.getvalue())

    def test_unexpected_paths_and_duplicate_archive_members_are_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as target:
            target.writestr("../manifest.json", "{}")
            target.writestr("project.json", "{}")
        with self.assertRaises(BundleError):
            inspect_bundle(output.getvalue())

        duplicate = io.BytesIO()
        with zipfile.ZipFile(duplicate, "w", compression=zipfile.ZIP_STORED) as target:
            target.writestr("manifest.json", "{}")
            target.writestr("manifest.json", "{}")
            target.writestr("project.json", "{}")
        with self.assertRaises(BundleError):
            inspect_bundle(duplicate.getvalue())


if __name__ == "__main__":
    unittest.main()
