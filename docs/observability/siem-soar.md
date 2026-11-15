# Security event delivery and SIEM/SOAR integration

VAOS can forward **redacted security events** through three opt-in transports: signed HTTPS webhooks, RFC 5424 Syslog over TLS, and OTLP/HTTP JSON logs. Delivery is off by default. Webhook and Syslog mappings include VAOS JSON, NDJSON, CEF 0, and ECS-shaped JSON. OCSF is deliberately not selectable yet: the current event source lacks required actor/source fields for a truthful, schema-valid mapping. This is protocol support, not a tested adapter for Splunk, Microsoft Sentinel, Elastic, Wazuh, or another vendor. VAOS does not run remote SOAR actions.

## What may leave the installation

Only the existing security-event record is forwarded: event identifier, UTC time, event type, severity, component, message, correlation identifier, and already-redacted details. Request bodies, camera images/frames, face data, model weights, training samples, credentials, and raw authorization headers are not sent. The queue is stored in the same local VAOS SQLite database. Do not put a password or token in an endpoint URL or path; VAOS stores the configured path locally while listing only the host.

Dashboard Basic-auth credential acceptance and denial are recorded as `auth.login_succeeded` and `auth.login_failed`. Because Basic credentials accompany each request, these events indicate an accepted/denied request rather than a distinct durable user session. Repeated results from the same effective client address are coalesced for 60 seconds in a capped process-local map; the address itself is not included in the event payload.

Other unhandled HTTP 401/403 responses are forwarded as `authz.request_denied`; a specific Basic-auth event suppresses a duplicate. Denials are coalesced for 60 seconds by client/status/method/route in a bounded in-memory map, while event details omit the client address, query string, request body and raw token. A valid but under-scoped API token can be represented by its stored token prefix as an actor reference. See [security and privacy](security.md#authentication-event-behavior) for bounds and limitations.

## Configure a destination

1. Open **Observability → SIEM & SOAR integrations** and select a transport.
2. Enter a public DNS endpoint. Signed HTTPS and OTLP use HTTPS port 443. Syslog TLS uses `syslog-tls://hostname` or `syslog-tls://hostname:6514`.
3. For signed HTTPS, set a random HMAC-SHA256 key of at least 32 UTF-8 bytes in the VAOS service environment, then enter only its environment-variable name (for example, `VAOS_SIEM_HMAC_KEY`). For OTLP, a bearer-token environment variable is optional. Do not paste secret values into the form. Syslog TLS uses CA and hostname validation and does not use the secret field.
4. If your organization uses a private receiver CA, set `VAOS_SIEM_CA_BUNDLE` in the VAOS service environment to an **absolute path** to a PEM CA bundle no larger than 4 MiB. VAOS adds those roots to the platform trust roots for SIEM HTTPS, OTLP/HTTP and Syslog TLS. Certificate-chain and endpoint-hostname verification remain required; an unreadable or invalid bundle fails closed and is never silently ignored. Keep the bundle in an administrator-controlled location. Changing the service environment requires restarting the service through its normal maintenance procedure.
5. Choose an event mapping for webhook or syslog: VAOS JSON, one-record-per-line NDJSON, CEF 0, or ECS-shaped JSON. OTLP always uses the OTLP Logs JSON mapping.
6. Keep the destination disabled while checking the name, receiver configuration and environment secret. Enable it only when ready. Choose **Send test event** to make a real, explicit outbound connection; the confirmation prompt is shown first. The test contains no user or camera data.
7. Monitor pending, retrying and dead-letter counts. Disable a destination to stop future sends; pending items are marked disabled and can be queued again if it is re-enabled.

The destination table provides **Edit**, enable/disable, test and remove actions. Editing never retrieves stored endpoint paths or credential references: re-enter the complete endpoint URL, then leave the credential field blank to keep its existing environment-variable reference when the transport stays the same. Changing transports clears the old reference. Updates apply to pending and future events. Saving an edit does not send a test event; use **Send test event** for a separate, confirmed outbound request.

Destinations must use public DNS names whose resolved addresses are all globally routable. VAOS rejects IP literals, localhost, private/reserved DNS answers, unexpected URL credentials/query/fragment and nonstandard ports. HTTPS connections pin the resolved address while validating TLS against the DNS hostname. Syslog TLS pins a public address and validates the receiver certificate and hostname. The platform trust store is always used; the optional `VAOS_SIEM_CA_BUNDLE` adds administrator-supplied PEM roots without disabling verification. Redirects are not followed.

## Supported wire formats

| Choice | Transport and encoding | Important limit |
| --- | --- | --- |
| VAOS JSON | Signed HTTPS `application/json` or JSON in the RFC 5424 `MSG` field | Stable VAOS event envelope; webhook uses HMAC-SHA256 over the timestamp and exact request bytes. |
| NDJSON | Signed HTTPS `application/x-ndjson` or one newline-terminated JSON event in syslog `MSG` | One queue item and one event per request/message. |
| CEF 0 | Signed HTTPS `application/cef` or CEF text in syslog `MSG` | Generic Common Event Format fields; no product-specific extension pack. |
| ECS | JSON fields using the Elastic Common Schema 8.11 field layout plus a `vaos` object for source details | Shape-level mapping only; it is not an Elastic integration or receiver certification. |
| OTLP | OTLP/HTTP `application/json` using the OpenTelemetry protobuf-JSON Logs request shape | OTLP has its own severity, resource and log-record mapping; CEF/ECS are not selectable for this transport. An optional bearer token is sent from the service environment. |

Syslog uses RFC 5424 messages with RFC 5425 octet-counted TLS framing on port 6514. TLS authenticates the remote server using the platform CA trust store plus any optional administrator-supplied `VAOS_SIEM_CA_BUNDLE`. Client-certificate authentication is not configured. Syslog has no application-level acknowledgement, so a disconnect during send can make acceptance uncertain and retries can duplicate an event.

### HTTPS signature verification

The signed webhook is a `POST` with `X-VAOS-Timestamp` and `X-VAOS-Signature: sha256=<hex>`. The HMAC-SHA256 input is the exact UTF-8 bytes `timestamp + "." + raw_request_body`. A receiver should compare signatures in constant time, reject stale timestamps (for example, older than five minutes), validate the chosen body format, and deduplicate by event ID. HMAC signing applies to each exact body, including CEF, ECS or NDJSON when selected.

### OTLP endpoint notes

Use an HTTPS OTLP logs endpoint such as `https://collector.example.com/v1/logs`. VAOS sends one redacted log record per queue item with resource attributes identifying VAOS, the event timestamp/severity, and source fields as attributes. The JSON request follows the OpenTelemetry Protocol's protobuf JSON mapping. VAOS accepts successful 2xx responses, recognizes JSON `partialSuccess.rejectedLogRecords`, caps the response read at 4 KiB, and applies the queue retry policy for failures. This source has not been tested against a real Collector or backend.

## Queue, retry and delivery semantics

- A background worker attempts at most two items in each approximately 30-second cycle. DNS resolution runs on a bounded pool of two daemon workers, waits at most three seconds per lookup, and has a queue of at most eight pending lookups. A timeout or full resolver queue fails that delivery attempt for normal retry; the underlying operating-system resolver call may continue in its isolated daemon worker until the OS returns. Each network connection also has a three-second timeout. Individual records are sent in separate requests/connections; this is not bulk batching. A 30-second database lease prevents ordinary overlapping workers, and a stale worker cannot overwrite a newer database result.
- Failed attempts use bounded exponential backoff and become dead-lettered after eight attempts. Destinations are capped at ten. The delivery outbox is capped at 50,000 rows overall and 5,000 pending/retrying rows per destination. Terminal history is pruned according to the administrator-configurable delivery-history period (90 days by default) and earlier if required by the total cap; when pending capacity is full, VAOS retains the local security event, skips external delivery, and increments a visible destination drop counter. No receiver response body or token is stored in delivery status.
- HTTP transports record the response status. Syslog records sent/failed state without pretending TLS has an HTTP-style acceptance code.
- The event timestamp is the source occurrence time; the retry schedule begins at the local enqueue time so clock skew or a future-dated source event does not delay its first delivery attempt.
- Delivery is at-least-once. A crash or long process pause after the remote endpoint accepts a message but before VAOS records success can cause a retry and duplicate. If both resolver workers remain stuck, the bounded lookup queue can fill; new attempts fail fast and retry later, while the two daemon calls remain isolated until the OS resolver returns. Consumers should deduplicate on event ID where the selected format supports it.
- These transports are network integrations. Tests mock external requests; no real external endpoint was contacted during implementation.

## API and permissions

`GET /api/admin/observability/integrations` lists host-only status. `POST` and `PUT` accept `display_name`, `transport`, `endpoint`, `event_format`, `secret_ref` (where appropriate), and optional `enabled`. On update, omit `secret_ref` to preserve the existing reference if transport is unchanged; changing transport clears the old reference unless a new reference is supplied. Supported transport values are `https_json`, `syslog_tls`, and `otlp`; supported non-OTLP event formats are `json`, `ndjson`, `cef`, and `ecs`. The default transport and event format are signed HTTPS JSON and JSON. `DELETE /api/admin/observability/integrations/{id}` archives and hides a destination, clears its endpoint and credential reference, and disables pending deliveries; local event and delivery history remains for audit, and any already in-flight send may finish. Removal is not reversible from the UI. Integration writes use administrator/control authorization; sensitive reads remain protected. MCP agents use the existing approval gate for sensitive integration changes and the separate Settings permission for deletion.

## Deferred OCSF mapping

The official OCSF 1.9.0 schema marks fields such as `actor`, `api`, and `src_endpoint` as required for API Activity. Authentication records also require a user identity and at least one of service or destination endpoint. VAOS currently has redacted event metadata but does not retain those identities/endpoints consistently. Adding invented placeholders would misstate who acted or where an action originated, so the OCSF choice is rejected. Revisit it after source instrumentation has consented, privacy-safe actor and endpoint references and the mapping passes the official schema validator.

## Vendor and certification limits

No Splunk, Sentinel, Elastic, Wazuh, syslog appliance, or OpenTelemetry Collector receiver was contacted in the isolated test run. Do not claim vendor compatibility or SOC compliance from these protocol-level tests. OCSF is not emitted and needs privacy-safe actor/source instrumentation plus official schema validation before it can be offered. SOAR commands, destination-specific templates, mutual TLS client certificates, per-destination trust configuration, and live receiver conformance remain future work.

## References

- [RFC 5424 — The Syslog Protocol](https://www.rfc-editor.org/rfc/rfc5424)
- [RFC 5425 — TLS Transport Mapping for Syslog](https://www.rfc-editor.org/rfc/rfc5425)
- [OpenTelemetry Protocol specification](https://opentelemetry.io/docs/specs/otlp/)
- [Elastic Common Schema reference](https://www.elastic.co/guide/en/ecs/current/ecs-reference.html)
- [OCSF API Activity schema (v1.9.0)](https://github.com/ocsf/ocsf-schema/blob/v1.9.0/events/application/api_activity.json)
- [OCSF Authentication schema (v1.9.0)](https://github.com/ocsf/ocsf-schema/blob/v1.9.0/events/iam/authentication.json)
