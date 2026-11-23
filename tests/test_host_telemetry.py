"""OS routing and driver counter validation without live hardware."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from host_telemetry import read_linux_gpu_metrics
from apple_gpu_metrics import read_apple_gpu_metrics


class HostTelemetryTests(unittest.TestCase):
    def test_apple_probe_is_skipped_on_other_platforms(self):
        with patch('apple_gpu_metrics.sys.platform', 'win32'), patch('apple_gpu_metrics.subprocess.run') as run:
            self.assertEqual(read_apple_gpu_metrics(), {})
            run.assert_not_called()

    def test_linux_driver_counters_and_temperature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / 'card0' / 'device'
            sensor = device / 'hwmon' / 'hwmon0'
            sensor.mkdir(parents=True)
            (device / 'gpu_busy_percent').write_text('42')
            (device / 'mem_info_vram_used').write_text('1048576')
            (device / 'mem_info_vram_total').write_text('8388608')
            (sensor / 'temp1_label').write_text('edge')
            (sensor / 'temp1_input').write_text('55000')
            with patch('host_telemetry.platform.system', return_value='Linux'):
                metrics = read_linux_gpu_metrics(root)
            self.assertEqual(metrics['gpu_utilization_percent'], 42)
            self.assertEqual(metrics['gpu_memory_used_mb'], 1)
            self.assertEqual(metrics['gpu_temperature_c'], 55)
            (device / 'gpu_busy_percent').write_text('NaN')
            with patch('host_telemetry.platform.system', return_value='Linux'):
                self.assertEqual(read_linux_gpu_metrics(root), {})

    def test_linux_probe_is_skipped_on_windows(self):
        with patch('host_telemetry.platform.system', return_value='Windows'):
            self.assertEqual(read_linux_gpu_metrics(), {})
