"""Hardware report tests with injected facts; no hardware probes are run."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hardware_intelligence import build_hardware_report


class HardwareIntelligenceTests(unittest.TestCase):
    def test_keeps_cuda_unknown_when_detector_is_not_loaded_and_redacts_data_path(self):
        with tempfile.TemporaryDirectory(prefix="vaos-hardware-secret-path-") as root:
            report = build_hardware_report(
                system_metrics={"memory_percent": 35, "memory_used_gb": 8, "memory_total_gb": 32},
                device_inventory={"cpu_threads": 12, "gpus": [{"index": 0, "name": "Example GPU"}]},
                inference_runtime={"object_model_loaded": False, "cuda_available": False},
                training_state={"status": "idle"},
                data_dir=Path(root),
            )
        self.assertTrue(report["compatibility"]["nvidia_device_detected"])
        self.assertEqual(report["compatibility"]["cuda_runtime_status"], "unknown")
        self.assertEqual(report["compatibility"]["cuda_inference_status"], "unknown")
        self.assertNotIn(root, str(report))
        self.assertEqual(report["facts"]["gpus"][0]["telemetry_measured"], False)

    def test_carries_each_measured_gpu_and_does_not_mutate_telemetry(self):
        with tempfile.TemporaryDirectory() as root:
            gpu_rows = [
                {"index": 0, "name": "GPU A", "utilization_percent": 75, "memory_used_mb": 4000, "memory_total_mb": 12000, "temperature_c": 62},
                {"index": 1, "name": "GPU B", "utilization_percent": 20, "memory_used_mb": 1000, "memory_total_mb": 8000, "temperature_c": 47},
            ]
            report = build_hardware_report(
                system_metrics={"gpu_devices": gpu_rows, "memory_percent": 50},
                device_inventory={"cpu_threads": 8, "gpus": [{"index": 0, "name": "GPU A"}, {"index": 1, "name": "GPU B"}]},
                inference_runtime={"object_model_loaded": True, "cuda_available": True, "cuda_runtime_available": True},
                training_state={"status": "idle"},
                data_dir=root,
            )
        self.assertEqual([gpu["name"] for gpu in report["facts"]["gpus"]], ["GPU A", "GPU B"])
        self.assertEqual(report["compatibility"]["cuda_inference_status"], "cuda")
        self.assertEqual(report["compatibility"]["cuda_runtime_status"], "available")
        self.assertEqual(len(gpu_rows), 2)

    def test_active_training_suppresses_competing_job_recommendation(self):
        with tempfile.TemporaryDirectory() as root:
            report = build_hardware_report(
                system_metrics={"memory_percent": 96, "gpu_devices": [{"index": 0, "name": "GPU", "utilization_percent": 99, "memory_used_mb": 11000, "memory_total_mb": 12000}]},
                device_inventory={"cpu_threads": 20, "gpus": [{"index": 0, "name": "GPU"}]},
                inference_runtime=None,
                training_state={"status": "running", "device": "GPU 0"},
                data_dir=root,
            )
        recommendations = report["recommendations"]
        self.assertIn("Do not start", recommendations["workload_guidance"])
        self.assertIn("high", recommendations["memory_guidance"])
        self.assertIsNone(recommendations["image_size_pixels"])
        self.assertFalse(recommendations["applied_automatically"])

    def test_reports_apple_mps_without_fabricating_gpu_utilization(self):
        with tempfile.TemporaryDirectory() as root:
            report = build_hardware_report(
                system_metrics={"memory_percent": 45},
                device_inventory={"cpu_threads": 10, "gpus": [], "mps_available": True},
                inference_runtime={"object_model_loaded": True, "cuda_available": False, "mps_available": True, "inference_backend": "mps"},
                training_state={"status": "idle"},
                data_dir=root,
            )
        compatibility = report["compatibility"]
        self.assertTrue(compatibility["mps_device_detected"])
        self.assertEqual(compatibility["inference_backend"], "mps")
        self.assertEqual(compatibility["mps_inference_status"], "active")
        self.assertEqual(report["facts"]["gpus"], [])
        self.assertIn("not measure", compatibility["mps_inference_explanation"].casefold())


if __name__ == "__main__":
    unittest.main()
