from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path

import start_vaos


class LocalLauncherContractTests(unittest.TestCase):
    def test_only_loopback_hosts_are_accepted(self) -> None:
        for host in ("localhost", "127.0.0.1", "::1", "[::1]"):
            with self.subTest(host=host):
                self.assertTrue(start_vaos.is_loopback_host(host))
        for host in ("0.0.0.0", "192.168.1.5", "example.org", ""):
            with self.subTest(host=host):
                self.assertFalse(start_vaos.is_loopback_host(host))

    def test_health_url_brackets_ipv6(self) -> None:
        self.assertEqual(start_vaos.health_url("127.0.0.1", 8000), "http://127.0.0.1:8000/health")
        self.assertEqual(start_vaos.health_url("::1", 8001), "http://[::1]:8001/health")

    def test_port_probe_does_not_claim_an_active_listener(self) -> None:
        import socket

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            host, port = listener.getsockname()
            self.assertFalse(start_vaos.port_is_available(host, port))

    def test_argument_validation_rejects_remote_bind_and_same_port(self) -> None:
        parser = start_vaos.build_parser()
        remote = parser.parse_args(["--admin-host", "0.0.0.0"])
        with self.assertRaises(SystemExit):
            start_vaos._validate_args(remote, parser)
        same_port = parser.parse_args(["--admin-port", "9000", "--camera-port", "9000"])
        with self.assertRaises(SystemExit):
            start_vaos._validate_args(same_port, parser)

    def test_launcher_starts_both_services_and_stops_only_its_children(self) -> None:
        def unused_port() -> int:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                return int(probe.getsockname()[1])

        admin_port = unused_port()
        camera_port = unused_port()
        while camera_port == admin_port:
            camera_port = unused_port()
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="vaos-cross-platform-launcher-") as temp_dir:
            command = [
                sys.executable,
                str(root / "start_vaos.py"),
                "--admin-port", str(admin_port),
                "--camera-port", str(camera_port),
                "--data-dir", str(Path(temp_dir) / "data"),
                "--no-browser",
                "--ready-timeout", "45",
            ]
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            launcher = subprocess.Popen(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            try:
                expected = {admin_port: "vaos-admin", camera_port: "vaos-camera"}
                health: dict[int, dict[str, object]] = {}
                deadline = time.monotonic() + 50
                while time.monotonic() < deadline and len(health) < 2:
                    if launcher.poll() is not None:
                        self.fail(f"The source launcher exited during startup with code {launcher.returncode}.")
                    for port, app_name in expected.items():
                        if port in health:
                            continue
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
                                self.assertEqual(response.status, 200)
                                self.assertEqual(payload.get("app"), app_name)
                                health[port] = payload
                        except (OSError, ValueError, urllib.error.URLError):
                            pass
                    if len(health) < 2:
                        time.sleep(0.2)
                self.assertEqual(set(health), {admin_port, camera_port})
            finally:
                if launcher.poll() is None:
                    if os.name == "nt":
                        launcher.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        launcher.send_signal(signal.SIGTERM)
                    try:
                        launcher.wait(timeout=18)
                    except subprocess.TimeoutExpired:
                        launcher.kill()
                        launcher.wait(timeout=5)
            self.assertEqual(launcher.returncode, 0)
            for port in (admin_port, camera_port):
                deadline = time.monotonic() + 5
                connect_result = 0
                while time.monotonic() < deadline:
                    with socket.socket() as probe:
                        probe.settimeout(0.3)
                        connect_result = probe.connect_ex(("127.0.0.1", port))
                    if connect_result != 0:
                        break
                    time.sleep(0.2)
                self.assertNotEqual(
                    connect_result,
                    0,
                    f"A VAOS child service is still listening on port {port} after shutdown.",
                )


if __name__ == "__main__":
    unittest.main()
