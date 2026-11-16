# Security and privacy guide

VAOS is local-first, but local storage is not automatically private or secure. The database and data directory may contain names, participant photos, face embeddings, camera/enrollment links, reviewed images, logs, model files and API/MCP configuration. Identify the data controller, purpose, access, retention and deletion process before deployment. Obtain required consent and legal review for biometric processing.

## Minimum safe deployment

- Keep the administrator site on loopback for local use. For remote access, use HTTPS and protect the admin hostname behind a VPN, identity-aware gateway or equivalent policy.
- Do not expose the admin service or local MCP stdio process through a public tunnel.
- Use a strong unique administrator password, protect secrets and configuration files, and rotate credentials if disclosed.
- Enable API/MCP only when needed. Use expiring, narrowly scoped tokens; new MCP installations start with Diagnostics only. Keep MCP read-only until a supervised task requires control.
- Review exact one-time MCP approval previews. Never approve an unclear request. MCP cannot approve its own actions or change its own policy.
- The MCP bridge uses a fixed configured API origin and does not follow HTTP redirects with its bearer token. Keep `VAOS_BASE_URL` pointed at the canonical VAOS API origin and use HTTPS remotely.
- Restrict and encrypt the data directory and backups. Delete expired exports and unneeded prepared datasets only after checking the exact target path.
- Review third-party model, package and dataset licenses separately from the VAOS Apache 2.0 license.

## Privacy and model limitations

Face matching is sensitive biometric processing. A similarity score is not a probability or identity proof. Validate false-match and false-nonmatch rates for the intended environment, lighting, camera and population. Do not use outputs as the sole basis for consequential or safety-critical decisions. Object detections are also fallible and should be reviewed for context and bias.

External observability delivery is opt-in. Review the destination and mapped event fields before enabling it. The optional MCP telemetry records fixed tool name, duration and outcome, not tool arguments or returned content; see [MCP details](MCP.md).

## Incident response

If a token, tunnel or invitation link is exposed, revoke/disable it, rotate dependent credentials, review access logs and notify affected administrators. If biometric or personal data is exposed, preserve relevant evidence without copying it into public issues, follow the organization’s incident and notification procedures, and seek applicable legal advice. Do not publish secrets or personal data in a vulnerability report.

## Vulnerability reports

Follow the [security policy](../SECURITY.md). Until the repository has a dedicated private reporting channel, contact the maintainer through the published project website and request a private route. Include version, impact and a minimal reproduction without personal/biometric data.
