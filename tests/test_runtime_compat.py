"""Cross-platform path, accelerator-selection and resource-profile contracts."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime_compat import (
    available_cpu_threads,
    default_datasets_dir,
    resolve_inference_backend,
    resolve_training_device,
    training_resource_profile,
)


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_dataset_directory_respects_override_on_all_platforms(self):
        self.assertEqual(
            default_datasets_dir("~/custom-data", system_name="Darwin", home="/Users/test"),
            Path("~/custom-data").expanduser(),
        )

    def test_dataset_directory_uses_native_default_by_platform(self):
        self.assertEqual(
            default_datasets_dir(system_name="Windows", home="C:/Users/test", windows_data_drive_exists=True),
            Path("D:/VAOS_Datasets"),
        )
        self.assertEqual(
            default_datasets_dir(system_name="Windows", home="C:/Users/test", windows_data_drive_exists=False),
            Path("C:/Users/test/VAOS_Datasets"),
        )
        self.assertEqual(
            default_datasets_dir(system_name="Darwin", home="/Users/test"),
            Path("/Users/test/VAOS_Datasets"),
        )
        self.assertEqual(
            default_datasets_dir(system_name="Linux", home="/home/test"),
            Path("/home/test/VAOS_Datasets"),
        )

    def test_cpu_thread_count_prefers_process_affinity(self):
        with patch.object(os, "process_cpu_count", create=True, return_value=3):
            self.assertEqual(available_cpu_threads(), 3)

    def test_auto_accelerator_prefers_cuda_then_mps_then_cpu(self):
        cuda = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True), backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)))
        mps = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False), backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)))
        cpu = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False), backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)))
        self.assertEqual(resolve_inference_backend(cuda)["backend"], "cuda")
        self.assertEqual(resolve_inference_backend(mps)["backend"], "mps")
        self.assertEqual(resolve_inference_backend(cpu)["backend"], "cpu")
        self.assertEqual(resolve_inference_backend(mps, "cpu")["device"], "cpu")

    def test_missing_requested_accelerator_falls_back_and_reports_it(self):
        cpu = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False), backends=SimpleNamespace())
        result = resolve_inference_backend(cpu, "mps")
        self.assertEqual(result["backend"], "cpu")
        self.assertFalse(result["requested_backend_available"])

    def test_training_device_selector_resolves_mps_backend(self):
        torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False),
            backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
        )
        target = resolve_training_device(torch, "mps", 10)
        self.assertEqual(target["device"], "mps")
        self.assertEqual(target["accelerator"], "mps")
        self.assertIsNone(target["cpu_threads"])

    def test_training_device_selector_rejects_unavailable_mps_without_silent_cpu_change(self):
        torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False),
            backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
        )
        with self.assertRaisesRegex(RuntimeError, "Apple MPS is unavailable"):
            resolve_training_device(torch, "mps", 8)

    def test_training_device_selector_reports_cuda_and_cpu_targets(self):
        torch = SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: True,
                device_count=lambda: 2,
                get_device_name=lambda index: f"Fixture GPU {index}",
            ),
            backends=SimpleNamespace(),
        )
        one_gpu = resolve_training_device(torch, "gpu:1", 8)
        self.assertEqual(one_gpu["device"], "1")
        self.assertEqual(one_gpu["accelerator"], "cuda")
        all_gpus = resolve_training_device(torch, "gpu:all", 8, resource_mode=True)
        self.assertEqual(all_gpus["device"], [0, 1])
        self.assertEqual(all_gpus["cpu_threads"], 7)

        cpu = resolve_training_device(torch, "cpu:all", 8, resource_mode=True)
        self.assertEqual(cpu["device"], "cpu")
        self.assertEqual(cpu["cpu_threads"], 7)

    def test_low_memory_never_forces_loader_workers_and_reduces_profile(self):
        profile = training_resource_profile(cpu_threads=16, available_ram_gb=2.2, accelerator="cpu", resource_mode=True)
        self.assertEqual(profile["workers"], 0)
        self.assertEqual(profile["batch"], 1)
        self.assertEqual(profile["image_size"], 320)

    def test_unknown_memory_uses_conservative_profile(self):
        profile = training_resource_profile(cpu_threads=8, available_ram_gb=None, accelerator="mps")
        self.assertEqual(profile["workers"], 0)
        self.assertEqual(profile["batch"], 2)
        self.assertEqual(profile["image_size"], 512)

    def test_single_cuda_resource_training_can_use_ultralytics_auto_batch(self):
        profile = training_resource_profile(cpu_threads=8, available_ram_gb=16, accelerator="cuda", gpu_count=1, resource_mode=True)
        self.assertEqual(profile["batch"], -1)
        multi = training_resource_profile(cpu_threads=8, available_ram_gb=16, accelerator="cuda", gpu_count=2, resource_mode=True)
        self.assertEqual(multi["batch"], 4)

    def test_training_device_picker_exposes_mps_conditionally(self):
        page = (Path(__file__).resolve().parents[1] / "templates" / "training.html").read_text(encoding="utf-8")
        self.assertIn("mps=Boolean(inventory?.mps_available)", page)
        self.assertIn("if(mps){", page)
        self.assertIn("Apple Silicon · Metal Performance Shaders", page)


if __name__ == "__main__":
    unittest.main()
