"""Isolated desktop/mobile browser smoke for the VAOS production UI."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ROUTES = (
    "/",
    "/settings",
    "/hardware",
    "/statistics",
    "/tracking",
    "/training",
    "/training/activity",
    "/datasets",
    "/video-review",
    "/projects",
    "/about",
    "/api-explorer",
)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_health(url: str, process: subprocess.Popen[bytes], name: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"The isolated {name} service exited before becoming healthy.")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                payload = json.loads(response.read())
            if payload.get("status") == "ok":
                return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"The isolated {name} service did not become healthy within 90 seconds.")


def start_service(module: str, port: int, env: dict[str, str], log_path: Path) -> subprocess.Popen[bytes]:
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", f"{module}:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    # Retain the handle so it can be closed only after the child is stopped.
    process._vaos_log_handle = log_file  # type: ignore[attr-defined]
    return process


def stop_services(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for process in reversed(processes):
        if process.poll() is None:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        handle = getattr(process, "_vaos_log_handle", None)
        if handle is not None:
            handle.close()


def main() -> int:
    screenshot_dir = Path(os.environ.get("VAOS_UI_ARTIFACT_DIR", tempfile.gettempdir()))
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    admin_port, camera_port = free_port(), free_port()
    while camera_port == admin_port:
        camera_port = free_port()
    password = secrets.token_urlsafe(32)
    processes: list[subprocess.Popen[bytes]] = []

    with tempfile.TemporaryDirectory(prefix="vaos-ui-smoke-") as temporary:
        data_dir = Path(temporary) / "data"
        common_env = os.environ.copy()
        common_env.update(
            {
                "VAOS_DATA_DIR": str(data_dir),
                "MODEL_DIR": str(data_dir / "models"),
                "YOLO_CONFIG_DIR": str(data_dir / "ultralytics"),
                "OBJECT_DETECTOR_DEVICE": "cpu",
                "CUDA_VISIBLE_DEVICES": "",
                "VAOS_CAMERA_INTERNAL_URL": f"http://127.0.0.1:{camera_port}",
                "VAOS_PUBLIC_CAMERA_URL": f"http://127.0.0.1:{camera_port}",
                "VAOS_ADMIN_PASSWORD": password,
            }
        )
        common_env.pop("CAMERA_SERVICE_IMPORT", None)
        camera_env = common_env.copy()
        camera_env["CAMERA_SERVICE_IMPORT"] = "1"
        try:
            camera = start_service("camera_service", camera_port, camera_env, Path(temporary) / "camera.log")
            processes.append(camera)
            wait_for_health(f"http://127.0.0.1:{camera_port}/health", camera, "camera")
            admin = start_service("main", admin_port, common_env, Path(temporary) / "admin.log")
            processes.append(admin)
            base_url = f"http://127.0.0.1:{admin_port}"
            wait_for_health(f"{base_url}/health", admin, "admin")

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    http_credentials={"username": "admin", "password": password},
                    viewport={"width": 1440, "height": 900},
                )
                page = context.new_page()
                page.set_default_timeout(10_000)
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))

                for route in ROUTES:
                    response = page.goto(f"{base_url}{route}", wait_until="domcontentloaded", timeout=30_000)
                    if response is None or response.status != 200:
                        raise RuntimeError(f"UI route {route} returned {response.status if response else 'no response'}.")
                    if not page.locator("body").inner_text().strip():
                        raise RuntimeError(f"UI route {route} rendered an empty page.")
                    if route in {"/", "/training", "/statistics", "/about"}:
                        page.screenshot(path=str(screenshot_dir / f"{route.strip('/').replace('/', '-') or 'control-room'}-desktop.png"), full_page=True)

                page.goto(f"{base_url}/", wait_until="domcontentloaded")
                page.locator('a[href="/about"]').first.click()
                if not page.url.endswith("/about"):
                    raise RuntimeError("The Control Room About VAOS navigation link did not open the About page.")
                metadata = json.loads((ROOT / "VAOS_PROJECT_METADATA.json").read_text(encoding="utf-8"))
                expected_links = {
                    "Creator website": metadata["creator"]["website"],
                    "LinkedIn profile": metadata["creator"]["linkedin"],
                }
                for name, target in expected_links.items():
                    link = page.get_by_role("link", name=name)
                    if link.get_attribute("href") != target:
                        raise RuntimeError(f"{name} does not match the centrally configured project metadata.")
                    rel = (link.get_attribute("rel") or "").casefold().split()
                    if not {"noopener", "noreferrer"}.issubset(rel):
                        raise RuntimeError(f"{name} is missing safe new-tab link attributes.")
                if not page.get_by_text(metadata["creator_statement"], exact=True).is_visible():
                    raise RuntimeError("The About page does not show the configured creator/community attribution.")

                page.keyboard.press("Tab")
                focused_tag = page.evaluate("document.activeElement && document.activeElement.tagName")
                if not focused_tag or focused_tag == "BODY":
                    raise RuntimeError("Keyboard focus did not reach a visible page control.")
                page.screenshot(path=str(screenshot_dir / "about-keyboard-desktop.png"), full_page=True)

                page.set_viewport_size({"width": 390, "height": 844})
                for route in ("/", "/about", "/training"):
                    page.goto(f"{base_url}{route}", wait_until="domcontentloaded", timeout=30_000)
                    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                    if overflow > 2:
                        raise RuntimeError(f"{route} has {overflow}px of horizontal overflow at 390px viewport width.")
                    if route in {"/", "/about"}:
                        page.screenshot(path=str(screenshot_dir / f"{route.strip('/') or 'control-room'}-mobile.png"), full_page=True)

                context.close()
                browser.close()
            if page_errors:
                raise RuntimeError("Browser JavaScript errors: " + " | ".join(page_errors[:8]))
            print(f"Browser smoke passed: {len(ROUTES)} routes, navigation, creator links, keyboard focus, and 390px overflow checks.")
            print(f"Screenshots written to {screenshot_dir}")
            return 0
        finally:
            stop_services(processes)


if __name__ == "__main__":
    raise SystemExit(main())
