from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADMIN_PAGES = (
    "api_explorer.html",
    "dataset_manager.html",
    "about.html",
    "hardware.html",
    "index.html",
    "live_system.html",
    "projects.html",
    "observability.html",
    "tracking.html",
    "training.html",
    "training_activity.html",
    "video_review.html",
)


class AdminUiShellContractTests(unittest.TestCase):
    def test_admin_pages_share_navigation_and_skip_to_content(self) -> None:
        for name in ADMIN_PAGES:
            with self.subTest(page=name):
                page = (ROOT / "templates" / name).read_text(encoding="utf-8")
                self.assertIn('{% include "_admin_navigation.html" %}', page)
                main_id = "obs-main" if name == "observability.html" else "app-main"
                self.assertRegex(page, rf'<main\b[^>]*id="{main_id}"[^>]*tabindex="-1"')
                expected_href = "#obs-main" if name == "observability.html" else "#app-main"
                self.assertRegex(
                    page,
                    rf'<a class="(?:observability|app)-skip-link" href="{re.escape(expected_href)}">Skip to main content</a>',
                )
                self.assertRegex(page, r"<h1\b[^>]*>")

    def test_shared_navigation_covers_every_admin_destination_and_marks_current_route(self) -> None:
        navigation = (ROOT / "templates" / "_admin_navigation.html").read_text(encoding="utf-8")
        for href in (
            "/",
            "/training",
            "/video-review",
            "/datasets",
            "/hardware",
            "/tracking",
            "/projects",
            "/about",
            "/settings",
            "/statistics",
            "/api-explorer",
        ):
            with self.subTest(href=href):
                self.assertIn(f'href="{href}"', navigation)
        self.assertIn('aria-current="page"', navigation)
        self.assertIn("request.query_params.get('view') == 'activity'", navigation)

    def test_settings_section_jump_targets_exist(self) -> None:
        page = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        for section in ("settings", "engine-settings", "system-settings", "api-mcp-settings", "model-enrichment"):
            with self.subTest(section=section):
                self.assertRegex(page, rf'<section\b[^>]*id="{re.escape(section)}"')
                self.assertIn(f'href="#{section}"', page)

    def test_mcp_settings_manual_is_bundled_and_reachable_from_settings(self) -> None:
        guide = ROOT / "docs" / "MCP_SETTINGS.md"
        self.assertTrue(guide.is_file())
        self.assertIn("human approval", guide.read_text(encoding="utf-8").casefold())
        settings = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/docs/MCP_SETTINGS.md"', settings)
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/docs/MCP_SETTINGS.md", include_in_schema=False)', main)
        self.assertIn("def mcp_settings_guide()", main)

    def test_api_explorer_endpoint_cards_use_theme_aware_contrast(self) -> None:
        page = (ROOT / "templates" / "api_explorer.html").read_text(encoding="utf-8")
        card_rule = re.search(r"\.explorer-operation\{([^}]+)\}", page)
        self.assertIsNotNone(card_rule)
        assert card_rule is not None
        self.assertIn("var(--ml-text", card_rule.group(1))
        self.assertIn("var(--ml-panel-raised", card_rule.group(1))
        self.assertIn(".explorer-operation:focus-visible", page)

    def test_tracking_fields_and_roi_have_consistent_layout(self) -> None:
        page = (ROOT / "templates" / "tracking.html").read_text(encoding="utf-8")
        self.assertIn(".tracking-form input:not([type=checkbox]):not([type=radio])", page)
        self.assertIn(".tracking-form .tracking-roi{grid-column:1/-1}", page)

    def test_primary_instruction_text_has_a_legible_floor(self) -> None:
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.training-page \.helper[^\{]+\{[^}]*font-size: 12px !important")
        self.assertRegex(css, r"\.settings-page \.helper[^\{]+\{[^}]*font-size: 12px !important")
        self.assertRegex(css, r"\.observability-page \.helper[^\{]+\{[^}]*font-size: 12px !important")

    def test_light_theme_observability_and_sidebar_text_have_contrast_overrides(self) -> None:
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        for selector in (
            ".theme-light .observability-page .result-box",
            ".theme-light .observability-page .observability-metric-row code",
            ".theme-light .training-sidebar .sidebar-system",
        ):
            with self.subTest(selector=selector):
                self.assertRegex(css, re.escape(selector) + r"\s*\{[^}]*color:")


if __name__ == "__main__":
    unittest.main()
