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
import urllib.error
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


def bearer_request(base_url: str, token: str, path: str, *, method: str = "GET", payload: dict | None = None, approval_id: str | None = None) -> tuple[int, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if approval_id:
        headers["X-VAOS-MCP-Approval-ID"] = approval_id
    request = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as response:
        try:
            return response.code, json.loads(response.read())
        finally:
            response.close()


def verify_mcp_settings_journey(page, base_url: str) -> None:
    """Exercise Settings, the MCP approval inbox, and token revocation in isolation."""
    response = page.goto(f"{base_url}/settings", wait_until="domcontentloaded", timeout=30_000)
    if response is None or response.status != 200:
        raise RuntimeError("The MCP Settings page did not load successfully.")
    page.locator("#api-token-list .empty-state").wait_for()
    if page.locator("#api-enabled").is_checked() or page.locator("#mcp-enabled").is_checked():
        raise RuntimeError("Fresh API and MCP access should be disabled by default.")
    if not page.locator("#mcp-read-only").is_checked():
        raise RuntimeError("Fresh MCP settings should start in read-only mode.")

    page.locator("#api-enabled").check()
    page.locator("#mcp-enabled").check()
    page.locator("#mcp-read-only").uncheck()
    page.locator("#mcp-sensitive").check()
    for checkbox in page.locator(".mcp-group").all():
        checkbox.uncheck()
    page.locator('.mcp-group[value="events"]').check()
    page.locator("#save-api-settings").click()
    page.get_by_text("API and MCP controls saved.", exact=False).wait_for()

    config_text = page.locator("#mcp-config-template").input_value()
    config = json.loads(config_text)
    env = config["mcpServers"]["vaos"]["env"]
    if env.get("VAOS_API_TOKEN") != "PASTE_A_READ_OR_CONTROL_TOKEN_HERE":
        raise RuntimeError("The MCP setup template must use a placeholder, never an issued token.")

    page.locator("#api-token-label").fill("CI isolated MCP approval journey")
    page.locator("#api-token-scope").select_option("control")
    for checkbox in page.locator(".api-token-group").all():
        checkbox.uncheck()
    page.locator('.api-token-group[value="events"]').check()
    page.locator("#api-token-mcp-agent").check()
    page.locator('#api-token-form button[type="submit"]').click()
    token_box = page.locator("#api-token-once")
    token_box.wait_for(state="visible")
    token = token_box.locator(".api-token-secret").inner_text().strip()
    if not token.startswith("vaos_") or "MCP approval gate: on" not in token_box.inner_text():
        raise RuntimeError("Settings did not issue a control token with the MCP approval gate enabled.")
    prefix = token[:14]

    rule_payload = {
        "name": "CI MCP approval smoke rule",
        "enabled": True,
        "rule_type": "count_in_region",
        "class_name": "person",
        "min_confidence": 0.35,
        "threshold_count": 1,
        "min_consecutive_frames": 3,
        "cooldown_seconds": 60,
        "dwell_seconds": 10,
        "camera_token": None,
        "region": {"x": 0, "y": 0, "width": 1, "height": 1},
    }
    status, denied = bearer_request(base_url, token, "/api/v1/admin/tracking/rules", method="POST", payload=rule_payload)
    approval = denied.get("detail", {})
    if status != 428 or not approval.get("approval_required") or not approval.get("request_id"):
        raise RuntimeError(f"MCP write was not safely held for approval (HTTP {status}).")

    page.locator("#refresh-mcp-approvals").click()
    approve_button = page.locator('[data-mcp-approval="approve"]').first
    approve_button.wait_for(state="visible")
    page.once("dialog", lambda dialog: dialog.accept())
    approve_button.click()
    page.locator("#mcp-approval-status").get_by_text("MCP action approved.", exact=False).wait_for()

    status, created = bearer_request(
        base_url, token, "/api/v1/admin/tracking/rules", method="POST",
        payload=rule_payload, approval_id=approval["request_id"],
    )
    if status != 200 or created.get("rule", {}).get("name") != rule_payload["name"]:
        raise RuntimeError(f"The exact approved MCP request could not be retried (HTTP {status}).")
    page.locator("#refresh-mcp-approvals").click()
    page.get_by_text("used", exact=True).wait_for()

    revoke_button = page.locator(f'[data-revoke-api-token="{prefix}"]')
    revoke_button.wait_for(state="visible")
    page.once("dialog", lambda dialog: dialog.accept())
    revoke_button.click()
    page.locator(".api-token-row").filter(has_text="CI isolated MCP approval journey").get_by_text("Revoked", exact=True).wait_for()
    status, rejected = bearer_request(base_url, token, "/api/v1/admin/tracking/rules")
    if status != 401 or "revoked" not in str(rejected).casefold():
        raise RuntimeError(f"A revoked MCP token remained usable (HTTP {status}).")


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
                profile_address = metadata["creator"]["linkedin"]
                visible_profile = page.locator(".creator-profile-address a")
                if not visible_profile.is_visible() or visible_profile.inner_text().strip() != profile_address:
                    raise RuntimeError("The About page does not visibly display the centrally configured LinkedIn address.")
                if visible_profile.get_attribute("href") != profile_address:
                    raise RuntimeError("The visible LinkedIn address does not link to the centrally configured profile.")

                page.keyboard.press("Tab")
                focused_tag = page.evaluate("document.activeElement && document.activeElement.tagName")
                if not focused_tag or focused_tag == "BODY":
                    raise RuntimeError("Keyboard focus did not reach a visible page control.")
                page.screenshot(path=str(screenshot_dir / "about-keyboard-desktop.png"), full_page=True)

                verify_mcp_settings_journey(page, base_url)
                page.goto(f"{base_url}/", wait_until="domcontentloaded", timeout=30_000)

                page.set_viewport_size({"width": 390, "height": 844})
                for route in ("/", "/about", "/training"):
                    page.goto(f"{base_url}{route}", wait_until="domcontentloaded", timeout=30_000)
                    if route == "/about" and not page.locator(".creator-profile-address a").is_visible():
                        raise RuntimeError("The LinkedIn creator address is not visible at the mobile viewport.")
                    if route in {"/", "/about"}:
                        page.screenshot(path=str(screenshot_dir / f"{route.strip('/') or 'control-room'}-mobile.png"), full_page=True)
                    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                    if overflow > 2:
                        offenders = page.evaluate("""() => [...document.querySelectorAll('body *')]
                          .map(el => { const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
                            return {tag:el.tagName.toLowerCase(),id:el.id,cls:typeof el.className==='string'?el.className:'',
                              left:Math.round(r.left),right:Math.round(r.right),width:Math.round(r.width),position:s.position}; })
                          .filter(x => x.right > innerWidth + 2 || x.left < -2)
                          .sort((a,b) => (b.right-innerWidth)-(a.right-innerWidth))
                          .slice(0,8)""")
                        raise RuntimeError(f"{route} has {overflow}px of horizontal overflow at 390px viewport width; wide elements: {offenders}")

                context.close()
                browser.close()
            if page_errors:
                raise RuntimeError("Browser JavaScript errors: " + " | ".join(page_errors[:8]))
            print(f"Browser smoke passed: {len(ROUTES)} routes, navigation, creator links, keyboard focus, MCP Settings approval/revocation, and 390px overflow checks.")
            print(f"Screenshots written to {screenshot_dir}")
            return 0
        finally:
            stop_services(processes)


if __name__ == "__main__":
    raise SystemExit(main())
