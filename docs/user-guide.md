# VAOS user guide

This guide explains the main VAOS workflows and gives extra detail for **Statistics & Observability**. VAOS is local-first: camera, review, model, database and operational records are kept on the configured VAOS host unless an administrator explicitly configures a remote service or integration.

## Start using VAOS

- For Windows setup, production installation, backup advice and configuration, follow the [main README](../README.md).
- For manual macOS/Linux startup and the CPU/CUDA/MPS support boundary, see [Platform support](PLATFORM_SUPPORT.md). For an AI-agent connection, see [MCP host setup](MCP_HOST_SETUP.md).
- For the purpose and boundaries of the application, see the [project overview](PROJECT_OVERVIEW.md).
- The admin dashboard is intended for an authorized administrator. Keep it behind a trusted local network, VPN or equivalent access control when remote.
- Review consent, biometric-data handling and deletion requirements before enrolling people or using face matching. Do not use predictions as safety-critical decisions without independent validation.

## Use Statistics & Observability

1. Sign in to the VAOS admin site and open **Statistics & observability** from the main navigation. In the source build, the page is `/statistics`.
2. Choose **Lifetime**, **Last 24 hours**, **Last 7 days**, **Last 30 days**, or a custom time range. Custom values are entered in your local time and converted to UTC for the request. The page labels its displayed timestamps and range boundaries.
3. Use **Installation overview** for the high-level counters and **Metric explorer** to search for a metric by name. Expand a group to inspect definitions, units, source, scope, and data availability; use **Expand all** or **Collapse all** to change all groups together.
4. To inspect recorded values, select a metric in **Metric history**. When supported, choose an exact dimension key and value. The chart and table describe recorded observations; queries are bounded and return at most 1,000 points.
5. Use **Application lifecycle** to review service start and stop records. An expired heartbeat is shown as **Unknown** unless VAOS has evidence for a clean stop or a verified failure. Unknown does not prove a crash.

### Understand missing or unusual values

- **Unavailable** means VAOS cannot support the measurement from recorded evidence. It is not zero.
- VAOS does not reconstruct activity from before its telemetry was enabled or from expired history. A counter can therefore be lower than lifetime activity on an older installation.
- API and camera/video timing measures server-side processing scope described in the metric definition; it is not automatically pure network or model-inference latency.
- Energy stays unavailable unless a supported sensor provides actual power readings. CPU/GPU utilization is not converted into wall energy.
- Training image and annotation counts describe the initialized dataset and successful label scan. They do not prove every image or box was consumed by the model trainer.

The full definitions and retention limits are in the [metric dictionary](observability/metrics-dictionary.md) and [energy measurement guide](observability/energy-measurement.md).

## Search operational activity

1. In **Log explorer**, enter any combination of text, severity, component, event type, or an exact correlation ID, then select **Search events**.
2. Use **Next page** to continue through the bounded result set. Open an event’s details when you need its safe, recorded metadata.
3. To watch new recorded events, select **Start live tail**; select it again to pause the tail. The live tail follows the same administrator access and retention rules.
4. Save a useful filter with a private name, then apply it later from **Saved filters**. Saved filters belong to the authenticated account and are limited to 20.
5. Choose **Prepare CSV report** or **Prepare PDF report** to create a filtered export. For shell pipelines, use the CLI examples below.

Correlation IDs are optional values supplied by callers, not a universal request or trace identifier. Some older log messages and events from components not yet instrumented will not appear in this explorer. See the [Log Explorer guide](observability/log-explorer.md) for filters, API details and retention limits.

## Create and download a report

Reports are prepared in the background. After requesting one, check **Reports & exports** for its progress and download when it completes. A report expires after 24 hours. Some report data requires an administrator session or an API token with the specific sensitive-read permission. Reports contain the data VAOS records; they are not backups and do not include camera images, face templates, model weights or dataset contents.

The [report guide](observability/reporting.md) lists report types, limits, permissions and coverage notes. If a report says that data is unavailable or incomplete, do not treat that as a measured zero.

## Optional external security-event delivery

External delivery is off by default. An administrator can configure a signed HTTPS, Syslog TLS or OTLP/HTTP destination in the **Integrations** area of the page. Review the endpoint and data being sent, then explicitly confirm any test event or enablement action. Keep secrets in the service’s protected configuration rather than in source control or a command line.

Only redacted security events are sent by this feature. Do not assume a vendor is supported or tested because a transport or event mapping exists. The current release evidence does not establish compatibility with a named SIEM vendor; see the [integration guide](observability/siem-soar.md) and [release-readiness report](observability/release-readiness.md).

## Use the optional terminal client

The `vaos` command-line client calls the same authenticated API and permissions as the dashboard. Install and configure it using the [CLI guide](observability/cli-reference.md). For example:

```sh
vaos status
vaos stats --period 7d
vaos logs search --component training --level ERROR --format ndjson
vaos logs tail --follow --component training
vaos reports list
vaos integrations status
```

The CLI does not bypass access controls or read arbitrary local files. Keep API tokens in a protected environment or secret store, and use HTTPS for remote VAOS servers.

## Troubleshooting

| What you see | What it means / what to try |
| --- | --- |
| Loading or service error | Confirm the admin service is available, then refresh. Do not assume a new source build is live until the service source/version has been verified. |
| Empty chart or no metric points | Check the time range and metric definition. The data may not have been recorded yet, may have expired, or may be unavailable on this hardware. |
| **Unknown** lifecycle end | VAOS observed a heartbeat expire without verified shutdown evidence. It cannot classify this as a confirmed crash. |
| Report failed or expired | Review its coverage/status message, retry after correcting the issue, and download completed reports before their 24-hour expiry. |
| Integration delivery failed | Confirm the public endpoint, TLS certificate and credentials, inspect delivery status, and leave delivery disabled until the destination is verified. No real vendor compatibility is implied. |

For implementation limits, recovery procedures and validation evidence, use the [Observability overview](observability/overview.md), [database and rollback guide](observability/database-schema.md), [validation record](observability/validation.md), and [current project status](PROJECT_STATUS.md).
