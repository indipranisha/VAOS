"""Static safety contracts for rendering untrusted observability records."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import observability_reports
import observability_store


class ObservabilityFrontendSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "static" / "observability.js"
        cls.javascript = source.read_text(encoding="utf-8")

    def test_untrusted_log_and_audit_values_are_rendered_as_text(self):
        for html_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            with self.subTest(html_sink=html_sink):
                self.assertNotIn(html_sink, self.javascript)

        self.assertIn("summary.textContent = item.message || 'View event details'", self.javascript)
        self.assertIn("pre.textContent = JSON.stringify(eventJson, null, 2)", self.javascript)
        self.assertIn("pre.textContent = JSON.stringify(item.details || {}, null, 2)", self.javascript)
        self.assertIn("option.textContent = item.name", self.javascript)

    def test_event_permalink_encodes_the_record_identifier(self):
        self.assertIn("encodeURIComponent(item.event_id)", self.javascript)

    def test_statistics_page_mobile_rules_contain_overflow_and_keep_navigation_targets_comfortable(self):
        root = Path(__file__).resolve().parents[1]
        stylesheet = (root / "static" / "style.css").read_text(encoding="utf-8")
        page = (root / "templates" / "observability.html").read_text(encoding="utf-8")
        tablet_rules = re.search(r"@media\(max-width:850px\)\s*\{([^\n]+)", stylesheet)
        mobile_styles = stylesheet[stylesheet.index("/* Statistics & Observability centre */"):]
        phone_rules = re.search(r"@media\(max-width:560px\)\s*\{([\s\S]*?)\n\}", mobile_styles)

        self.assertIsNotNone(tablet_rules)
        self.assertIn(".observability-page", tablet_rules.group(1))
        self.assertIn("max-width: 100%", tablet_rules.group(1))
        self.assertIn("overflow-x: clip", tablet_rules.group(1))
        self.assertIn("overflow-wrap: anywhere", tablet_rules.group(1))
        self.assertIsNotNone(phone_rules)
        self.assertIn(".observability-page .section-title-row", phone_rules.group(1))
        self.assertIn("flex-wrap: wrap", phone_rules.group(1))
        self.assertIn("white-space: normal", phone_rules.group(1))
        self.assertRegex(stylesheet, r"\.training-sidebar \.nav-link \{[^}]*min-height: 44px;[^}]*font-size: 12px")
        self.assertIn(".observability-header { width: 100%; min-width: 0; flex-direction: column", phone_rules.group(1))
        self.assertIn(".training-sidebar .primary-nav { width: 100%; min-width: 0; max-width: 100%", phone_rules.group(1))
        self.assertIn(".training-sidebar .nav-link { display: grid; grid-template-columns: 16px minmax(0,1fr)", phone_rules.group(1))
        shared_navigation = (Path(__file__).resolve().parents[1] / "templates" / "_admin_navigation.html").read_text(encoding="utf-8")
        nav_links = re.findall(r'<a class="nav-link[^\"]*"[^>]*>(.*?)</a>', shared_navigation, flags=re.DOTALL)
        self.assertEqual(len(nav_links), 12)
        self.assertTrue(all('class="nav-label"' in item for item in nav_links))
        self.assertIn(".training-sidebar .nav-label { display: block; width: 100%; min-width: 0; max-width: 100%", phone_rules.group(1))
        self.assertIn("style.css?v=vaos-1.1.0-ui-accessibility", page)

    def test_statistics_page_provides_keyboard_skip_navigation_and_screen_reader_captions(self):
        root = Path(__file__).resolve().parents[1]
        stylesheet = (root / "static" / "style.css").read_text(encoding="utf-8")
        page = (root / "templates" / "observability.html").read_text(encoding="utf-8")

        self.assertRegex(page, r'<a class="observability-skip-link" href="#obs-main">Skip to main content</a>')
        self.assertRegex(page, r'<main id="obs-main"[^>]*tabindex="-1"')
        self.assertRegex(stylesheet, r"\.observability-skip-link\s*\{[^}]*position:\s*fixed;[^}]*transform:\s*translateY")
        self.assertRegex(stylesheet, r"\.observability-skip-link:focus\s*\{[^}]*transform:\s*none;")
        self.assertRegex(stylesheet, r"\.sr-only\s*\{[^}]*clip:\s*rect\(0,0,0,0\)")

    def test_counter_storage_health_has_an_accessible_visible_status(self):
        page = (Path(__file__).resolve().parents[1] / "templates" / "observability.html").read_text(encoding="utf-8")
        self.assertIn('id="obs-counter-queue-status" class="result-box" role="status" aria-live="polite"', page)
        self.assertIn("api('/api/admin/observability/counter-queue')", self.javascript)
        self.assertIn("function renderCounterQueueStatus(data)", self.javascript)
        self.assertIn("No backlog observed", self.javascript)
        self.assertIn("Counter recovery status could not be checked", self.javascript)
        self.assertIn("Select Refresh to try again", self.javascript)
        self.assertIn("this warning does not confirm data loss", self.javascript)
        self.assertIn("This is an admin-process snapshot; it does not verify storage writes or camera-worker memory.", self.javascript)
        self.assertIn("Recovery pending", self.javascript)
        self.assertIn("may be lost if the service exits before storage recovers", self.javascript)
        self.assertIn("have mixed dimensions because a queue reached its detail limit", self.javascript)

    def test_async_filters_replace_stale_results_and_queue_refreshes(self):
        self.assertIn("state.logController?.abort()", self.javascript)
        self.assertIn("state.seriesController?.abort()", self.javascript)
        self.assertIn("requestId !== state.logRequestId", self.javascript)
        self.assertIn("requestId !== state.seriesRequestId", self.javascript)
        self.assertIn("state.refreshPending = true", self.javascript)

    def test_pausing_live_tail_cancels_and_invalidates_an_inflight_poll(self):
        start = self.javascript.index("function setLogTail(active)")
        end = self.javascript.index("async function loadSecurityAudit", start)
        implementation = self.javascript[start:end]
        self.assertIn("const wasActive = state.logTailActive", implementation)
        self.assertIn("if (!active && wasActive)", implementation)
        self.assertIn("state.logRequestId += 1", implementation)
        self.assertIn("state.logController?.abort()", implementation)
        self.assertIn("state.logController = null", implementation)
        self.assertIn("state.logBusy = false", implementation)
        self.assertLess(implementation.index("state.logRequestId += 1"), implementation.index("state.logController?.abort()"))
        self.assertIn("Promise.allSettled", self.javascript)
        page = (Path(__file__).resolve().parents[1] / "templates" / "observability.html").read_text(encoding="utf-8")
        self.assertIn('id="obs-session-message"', page)

    def test_metric_chart_accessible_description_tracks_filter_data_and_empty_states(self):
        root = Path(__file__).resolve().parents[1]
        page = (root / "templates" / "observability.html").read_text(encoding="utf-8")
        self.assertIn('role="img" aria-labelledby="obs-chart-title obs-chart-desc"', page)
        self.assertIn('id="obs-chart-title"', page)
        self.assertIn('id="obs-chart-desc"', page)
        self.assertIn("function clearSeries(message)", self.javascript)
        self.assertIn("state.seriesController?.abort()", self.javascript)
        self.assertIn("state.seriesRequestId += 1", self.javascript)
        self.assertIn("title.textContent = `${metricName} history · ${seriesLabel}`", self.javascript)
        self.assertIn("filtered by ${JSON.stringify(dimensions)}", self.javascript)
        self.assertIn("none had both a usable timestamp and numeric value to plot", self.javascript)
        self.assertIn("text.textContent = 'No plottable observations'", self.javascript)
        self.assertIn("if (!metricKey) { clearSeries('Select a metric to load its history.'); return; }", self.javascript)

    def test_metric_dictionary_covers_every_registered_metric(self):
        dictionary = (Path(__file__).resolve().parents[1] / "docs" / "observability" / "metrics-dictionary.md").read_text(encoding="utf-8")
        rows = [line for line in dictionary.splitlines() if re.match(r"^\|\s*`[^`]+`\s*\|", line)]
        documented_keys = [re.match(r"^\|\s*`([^`]+)`\s*\|", line).group(1) for line in rows]

        self.assertEqual(len(documented_keys), len(set(documented_keys)), "metric keys must appear once in the dictionary")
        self.assertSetEqual(set(documented_keys), set(observability_store.METRIC_CATALOGUE))
        for index, line in enumerate(rows):
            with self.subTest(metric=documented_keys[index]):
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                self.assertEqual(len(cells), 7, "each metric row must retain all dictionary columns")
                self.assertTrue(all(cells[1:]), "each metric must document units, source, dimensions, aggregation, quality and privacy")

    def test_observability_documentation_local_links_resolve(self):
        docs_root = Path(__file__).resolve().parents[1] / "docs" / "observability"
        broken = []
        for document in docs_root.rglob("*.md"):
            for raw_target in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                target = raw_target.strip().split()[0].strip("<>")
                if not target or target.startswith(("#", "https://", "http://", "mailto:", "tel:")):
                    continue
                path = target.split("#", 1)[0].split("?", 1)[0]
                if path and not (document.parent / path).resolve().exists():
                    broken.append((str(document.relative_to(docs_root)), target))

        self.assertEqual(broken, [], "all relative links in observability documentation must resolve")

    def test_every_observability_http_handler_is_in_the_function_catalogue(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
        catalogue = (root / "docs" / "observability" / "source-function-catalogue.md").read_text(encoding="utf-8")
        marker = "## Admin route handler inventory — `main.py`"
        self.assertIn(marker, catalogue)
        inventory = catalogue.split(marker, 1)[1].split("\n## ", 1)[0]
        route_handlers = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr not in {"get", "post", "put", "patch", "delete"} or not decorator.args:
                    continue
                path = decorator.args[0]
                if isinstance(path, ast.Constant) and isinstance(path.value, str) and "/observability/" in path.value:
                    route_handlers.add(node.name)

        missing = sorted(name for name in route_handlers if f"`{name}`" not in inventory)
        self.assertEqual(missing, [], "every observability HTTP handler must be mapped to its source catalogue")

    def test_hidden_observability_hooks_are_documented_outside_openapi(self):
        api = (Path(__file__).resolve().parents[1] / "docs" / "API.md").read_text(encoding="utf-8")
        self.assertIn("Internal observability hooks (not public or versioned API)", api)
        self.assertIn("GET /api/admin/observability/counter-queue", api)
        self.assertIn("POST /api/admin/observability/mcp/invocations", api)
        self.assertIn("They have no `/api/v1` alias", api)

    def test_metric_explorer_controls_grouping_aggregation_view_and_central_exports(self):
        root = Path(__file__).resolve().parents[1]
        page = (root / "templates" / "observability.html").read_text(encoding="utf-8")
        for control in (
            'id="obs-series-group-by"', 'id="obs-series-aggregation"', 'id="obs-series-interval"',
            'id="obs-series-export-csv"', 'id="obs-series-export-pdf"', 'id="obs-series-legend"',
            'id="obs-series-view-chart"', 'id="obs-series-view-table"',
        ):
            with self.subTest(control=control):
                self.assertIn(control, page)
        self.assertIn("metric?.series_aggregations", self.javascript)
        self.assertIn("params.set('aggregation', aggregation)", self.javascript)
        self.assertIn("params.set('interval', interval)", self.javascript)
        self.assertIn("params.set('group_by', groupBy)", self.javascript)
        self.assertIn("function setSeriesView(view)", self.javascript)
        self.assertIn("state.seriesView = view === 'table' ? 'table' : 'chart'", self.javascript)
        self.assertIn("function createMetricSeriesReport(format)", self.javascript)
        self.assertIn("report_type: 'metric-series'", self.javascript)
        self.assertIn("api('/api/admin/observability/reports/jobs'", self.javascript)
        self.assertIn("grouped by ${groupBy}", self.javascript)
        self.assertIn("allSeries.slice(0, 8)", self.javascript)
        self.assertIn("const displayedPoints = displayedSeries.flatMap(([, seriesPoints]) => seriesPoints)", self.javascript)
        self.assertIn("const values = displayedPoints.map((point) => point.value)", self.javascript)
        self.assertIn("const times = displayedPoints.map((point) => point.time)", self.javascript)
        self.assertIn("${displayedPoints.length} plotted", self.javascript)
        self.assertIn("groupCount: result.group_count, truncated: result.truncated", self.javascript)
        self.assertIn("query group(s) have no points on this returned page", self.javascript)
        self.assertIn("the response is limited to ${points.length}", self.javascript)
        self.assertIn("lower-count group(s) omitted from this chart", self.javascript)
        self.assertIn("$('obs-series-legend').replaceChildren()", self.javascript)

    def test_session_filter_is_carried_to_search_saved_filters_and_reports(self):
        page = (Path(__file__).resolve().parents[1] / "templates" / "observability.html").read_text(encoding="utf-8")
        self.assertIn('id="obs-log-session-id"', page)
        self.assertIn("params.set('session_id', sessionId)", self.javascript)
        self.assertIn("['session_id', 'obs-log-session-id']", self.javascript)
        self.assertIn("filters.session_id = sessionId", self.javascript)

    def test_actor_reference_filter_is_exposed_only_in_security_audit_and_reports(self):
        page = (Path(__file__).resolve().parents[1] / "templates" / "observability.html").read_text(encoding="utf-8")
        self.assertIn('id="obs-audit-actor-ref" maxlength="120"', page)
        self.assertIn('id="obs-report-actor-ref" maxlength="120"', page)
        self.assertIn("['actor_ref', 'obs-audit-actor-ref']", self.javascript)
        self.assertIn("filters.actor_ref = actorRef", self.javascript)
        self.assertIn("type !== 'security-events'", self.javascript)

    def test_report_guidance_matches_whether_the_report_needs_a_date_range(self):
        self.assertIn("function reportPrompt(type)", self.javascript)
        self.assertIn("type === 'lifetime-statistics'", self.javascript)
        self.assertIn("Choose a format, then select Generate report.", self.javascript)
        self.assertIn("Choose a date range and format, then select Generate report.", self.javascript)
        self.assertIn("$('obs-report-message').textContent = reportPrompt(type);", self.javascript)
        self.assertIn("reportPrompt(requestedDefinition.report_type)", self.javascript)

    def test_audit_integrity_button_is_wired_to_authenticated_check_and_status(self):
        page = (Path(__file__).resolve().parents[1] / "templates" / "observability.html").read_text(encoding="utf-8")
        self.assertIn('id="obs-audit-verify"', page)
        self.assertIn('id="obs-audit-integrity"', page)
        self.assertIn("$('obs-audit-verify').addEventListener('click'", self.javascript)
        self.assertIn("api('/api/admin/observability/security-audit/integrity')", self.javascript)
        self.assertIn("Integrity valid · ${result.checked} record(s) checked.", self.javascript)
        self.assertIn("Integrity check failed at record ${result.failed_sequence}", self.javascript)

    def test_retention_controls_explain_irreversibility_and_require_explicit_confirmation(self):
        page = (Path(__file__).resolve().parents[1] / "templates" / "observability.html").read_text(encoding="utf-8")
        for control in (
            'id="obs-retention-form"', 'id="obs-retention-events"',
            'id="obs-retention-metrics"', 'id="obs-retention-deliveries"',
            'id="obs-retention-save"', 'id="obs-retention-apply"',
        ):
            with self.subTest(control=control):
                self.assertIn(control, page)
        self.assertIn("A shorter period can permanently redact or remove older operational details", page)
        self.assertIn("Lifetime counters and the append-only security audit are kept.", page)
        self.assertIn("A shorter retention period can permanently redact or remove older operational details", self.javascript)
        self.assertIn("confirm_shorter: shorter", self.javascript)
        self.assertIn("confirm_expired_data_redaction: true", self.javascript)
        self.assertIn("api('/api/admin/observability/retention')", self.javascript)
        self.assertIn("retentionDirty: false", self.javascript)
        self.assertIn("if (!state.retentionDirty)", self.javascript)
        self.assertIn("Your unsaved selections are preserved", self.javascript)
        self.assertIn("Save your retention changes before applying cleanup", self.javascript)
        self.assertIn("addEventListener('change', markRetentionPolicyDirty)", self.javascript)
        self.assertIn("Generated report files are separate copies", page)
        self.assertIn("remain available until their own 24-hour expiry", self.javascript)

    def test_retention_save_and_cleanup_lock_controls_against_refresh_races(self):
        self.assertIn("retentionRequestId: 0", self.javascript)
        load_start = self.javascript.index("async function loadRetentionPolicy()")
        load_end = self.javascript.index("function syncRetentionControls()", load_start)
        load_policy = self.javascript[load_start:load_end]
        self.assertLess(load_policy.index("if (state.retentionBusy) return;"), load_policy.index("await api('/api/admin/observability/retention')"))
        self.assertIn("if (state.retentionBusy || requestId !== state.retentionRequestId) return;", load_policy)
        self.assertEqual(self.javascript.count("state.retentionRequestId += 1;"), 2)
        self.assertIn("function syncRetentionControls()", self.javascript)
        self.assertIn("for (const id of ['obs-retention-events', 'obs-retention-metrics', 'obs-retention-deliveries', 'obs-retention-save']) $(id).disabled = busy", self.javascript)
        self.assertIn("state.retentionBusy = true; syncRetentionControls();", self.javascript)
        self.assertIn("state.retentionBusy = false; syncRetentionControls();", self.javascript)
        self.assertIn("Reports are generated in the background", (Path(__file__).resolve().parents[1] / "templates" / "observability.html").read_text(encoding="utf-8"))

    def test_feature_pages_link_to_registered_central_reports(self):
        root = Path(__file__).resolve().parents[1]
        shortcuts = {
            "templates/index.html": ("lifetime-statistics", "cameras", "models", "api-activity", "mcp-activity", "security-events"),
            "templates/api_explorer.html": ("api-activity",),
            "templates/training.html": ("training",),
            "templates/training_activity.html": ("training",),
            "templates/dataset_manager.html": ("datasets",),
            "templates/hardware.html": ("hardware-energy",),
            "templates/tracking.html": ("tracking-events",),
            "templates/video_review.html": ("inference",),
            "templates/live_system.html": ("cameras",),
        }
        for relative_path, report_types in shortcuts.items():
            with self.subTest(page=relative_path):
                page = (root / relative_path).read_text(encoding="utf-8")
                for report_type in report_types:
                    with self.subTest(report=report_type):
                        self.assertIn(report_type, observability_reports.REPORT_TYPES)
                        self.assertIn(f"/statistics?report={report_type}", page)

        self.assertIn("new URLSearchParams(window.location.search).get('report')", self.javascript)
        self.assertIn("state.reportTypes.find((item) => item.report_type === requestedReport)", self.javascript)
        self.assertIn("Choose a date range and format, then select Generate report.", self.javascript)


if __name__ == "__main__":
    unittest.main()
