# Frontend page and asset catalogue

VAOS currently uses FastAPI-served HTML templates with JavaScript and CSS assets, not a separately built SPA. Page handlers and much of the service logic live in `main.py`; the camera application is in `camera_service.py`. Navigation is still repeated across several templates, so a change to global navigation or shared layout should be checked across all pages.

## Main pages

| User-facing page | Template / primary assets | Main purpose |
| --- | --- | --- |
| Control Room/dashboard | `templates/index.html` | Camera overview, status, people/events and engine controls. |
| Settings | `/settings` route reuses `templates/index.html` with settings-only context | Application, API/MCP, tunnel and service settings. |
| ML Training | `templates/training.html` | Dataset selection, sample review, training start and status; current page behavior is implemented in the template rather than a dedicated `training.js` asset. |
| Training Activity | `templates/training_activity.html` | Training log and per-epoch activity view. |
| Dataset manager | `templates/dataset_manager.html` | Dataset import and preparation workflows. |
| Video Review | `templates/video_review.html` | Video-review sessions and reviewed detection samples. |
| Hardware Intelligence | `templates/hardware.html` | Measured hardware facts and clearly labeled estimates. |
| Statistics & Observability | `templates/observability.html`, `static/observability.js` | Metrics, logs, lifecycle, reports and optional integrations. |
| Tracking & Event Rules | `templates/tracking.html`, `static/tracking.js` | Object-count/dwell rules and event history. |
| API Explorer | `templates/api_explorer.html`, `static/api_explorer.js` | Protected generated OpenAPI endpoint explorer. |
| Project Bundles | `templates/projects.html`, `static/projects.js` | Import/export of constrained configuration bundles. |
| About VAOS | `templates/about.html` | Product and creator/community attribution. |
| Camera and enrollment pages | `templates/camera.html`, `templates/enroll.html` | Browser frame capture and consent-based enrollment. |

## Shared styles and interaction

`static/style.css` contains the shared visual rules; page-specific inline styles/scripts remain in several templates. `templates/_creator_attribution.html` holds reusable footer attribution. Before editing a page, inspect both its template and any page-specific script, then search for shared navigation and component duplicates. Avoid duplicating API calls or writing secrets into browser storage or URLs.

## Accessibility and responsive QA

The current source does not have a complete automated browser/accessibility suite. A page rendering test or Jinja parse is not proof of keyboard accessibility or mobile usability. For UI changes, manually check keyboard order/focus, form labels/errors, loading/empty states, zoom, 320px and 390px widths, and desktop overflow. Capture screenshots and browser/version details when recording evidence. See the outstanding browser and release gates in [TESTING.md](TESTING.md).
