"""Run the local VAOS admin and camera services on Windows, macOS, or Linux.

This source launcher is intentionally loopback-only and does not manage public
tunnels. It owns only the child processes it starts and never kills a process
that already occupies a VAOS port.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from project_metadata import PROJECT_METADATA

APP_ROOT = Path(__file__).resolve().parent
EXPECTED_APPS = (("admin", "main:app", "vaos-admin"), ("camera", "camera_service:app", "vaos-camera"))


def is_loopback_host(host: str) -> bool:
    """Accept only localhost/IP loopback for the local development launcher."""
    normalized = host.strip().strip("[]").casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def port_is_available(host: str, port: int) -> bool:
    """Return whether a local TCP port can be bound without changing it."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def health_url(host: str, port: int) -> str:
    """Build an HTTP URL with IPv6 brackets when required."""
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{display_host}:{port}/health"


def read_health(url: str, timeout: float = 1.0) -> dict[str, Any] | None:
    """Return a health payload or None while the service is not ready."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start VAOS locally on Windows, macOS, or Linux.")
    parser.add_argument("--admin-host", default="127.0.0.1", help="Admin bind address (loopback only).")
    parser.add_argument("--admin-port", type=int, default=None,
                        help="Admin port; defaults to a random available 8xx0 port (xx=11–99).")
    parser.add_argument("--camera-host", default="127.0.0.1", help="Camera bind address (loopback only).")
    parser.add_argument("--camera-port", type=int, default=8001)
    parser.add_argument("--data-dir", type=Path, help="Optional VAOS_DATA_DIR override; defaults to the app's data folder.")
    parser.add_argument("--ready-timeout", type=float, default=60.0, help="Seconds to wait for each health endpoint.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the dashboard after startup.")
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    for attribute in ("admin_host", "camera_host"):
        host = str(getattr(args, attribute)).strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not is_loopback_host(host):
            parser.error("The local launcher binds to loopback only. Use a protected production proxy for remote access.")
        setattr(args, attribute, host)
    for port in (args.admin_port, args.camera_port):
        if port is not None and not 1 <= port <= 65535:
            parser.error("Ports must be between 1 and 65535.")
    if args.admin_port is None:
        candidates = [8000 + xx * 10 for xx in range(11, 100)]
        random.shuffle(candidates)
        args.admin_port = next(
            (port for port in candidates
             if port != args.camera_port and port_is_available(args.admin_host, port)),
            None,
        )
        if args.admin_port is None:
            parser.error("No available admin port matching 8xx0 (xx=11–99).")
    if args.admin_port == args.camera_port and args.admin_host == args.camera_host:
        parser.error("Admin and camera services need different ports.")
    if not 1 <= args.ready_timeout <= 600:
        parser.error("--ready-timeout must be between 1 and 600 seconds.")


def _stop_owned_processes(processes: list[subprocess.Popen[bytes]]) -> None:
    """Stop only children created by this launcher; never search/kill by name."""
    for process in reversed(processes):
        if process.poll() is not None:
            continue
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGTERM)
        except (OSError, ValueError):
            process.terminate()
    deadline = time.monotonic() + 8.0
    for process in reversed(processes):
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def _handle_termination_signal(_signum: int, _frame: Any) -> None:
    """Convert process stop signals into a cleanup-safe Python interruption."""
    raise KeyboardInterrupt


def _start_service(
    *, name: str, module: str, expected_app: str, host: str, port: int,
    env: dict[str, str], logs_dir: Path, timeout: float,
) -> subprocess.Popen[bytes]:
    log_file = (logs_dir / f"{name}_service.log").open("ab")
    error_file = (logs_dir / f"{name}_service_error.log").open("ab")
    command = [sys.executable, "-m", "uvicorn", module, "--host", host, "--port", str(port)]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            cwd=APP_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=error_file,
            close_fds=os.name != "nt",
            creationflags=creationflags,
        )
    finally:
        log_file.close()
        error_file.close()

    try:
        deadline = time.monotonic() + timeout
        url = health_url(host, port)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{name} service exited with code {process.returncode}; see {logs_dir}.")
            health = read_health(url)
            if health:
                if health.get("app") != expected_app:
                    raise RuntimeError(f"Unexpected service answered at {url}; expected {expected_app}.")
                return process
            time.sleep(0.25)
        raise RuntimeError(f"{name} service did not become healthy within {timeout:g}s; see {logs_dir}.")
    except BaseException:
        _stop_owned_processes([process])
        raise


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(args, parser)
    endpoints = ((args.admin_host, args.admin_port), (args.camera_host, args.camera_port))
    for host, port in endpoints:
        if not port_is_available(host, port):
            parser.error(f"Port {port} on {host} is already in use. The launcher will not stop or replace another process.")

    data_dir = args.data_dir.expanduser().resolve() if args.data_dir else None
    if data_dir:
        data_dir.mkdir(parents=True, exist_ok=True)
    log_root = data_dir or APP_ROOT / "data"
    logs_dir = log_root / "logs" / f"local-launcher-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    logs_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if data_dir:
        env["VAOS_DATA_DIR"] = str(data_dir)

    processes: list[subprocess.Popen[bytes]] = []
    previous_signal_handlers: dict[signal.Signals, Any] = {}
    for stop_signal in (signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if stop_signal is not None:
            previous_signal_handlers[stop_signal] = signal.getsignal(stop_signal)
            signal.signal(stop_signal, _handle_termination_signal)
    try:
        for (name, module, expected), (host, port) in zip(EXPECTED_APPS, endpoints):
            process = _start_service(
                name=name,
                module=module,
                expected_app=expected,
                host=host,
                port=port,
                env=env,
                logs_dir=logs_dir,
                timeout=args.ready_timeout,
            )
            processes.append(process)
            print(f"VAOS {name} service ready at http://{host}:{port}", flush=True)
        dashboard = f"http://{args.admin_host}:{args.admin_port}"
        print(f"VAOS version: {PROJECT_METADATA['version']}\nDashboard: {dashboard}\nLogs: {logs_dir}", flush=True)
        if env.get("VAOS_ENV", "development").strip().casefold() == "staging":
            print("Staging username: admin\nStaging password: 1122334455", flush=True)
        print("Press Ctrl+C to stop these VAOS services.", flush=True)
        if not args.no_browser:
            webbrowser.open(dashboard)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        for process in processes:
            if process.poll() is not None:
                print(f"A VAOS service exited (code {process.returncode}); stopping the other service.", file=sys.stderr)
                return int(process.returncode or 1)
        return 0
    except KeyboardInterrupt:
        print("\nStopping VAOS services started by this launcher...", flush=True)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"VAOS startup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        _stop_owned_processes(processes)
        for stop_signal, previous_handler in previous_signal_handlers.items():
            signal.signal(stop_signal, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
