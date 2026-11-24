# Security policy

VAOS is a local-first application that can store face templates, camera enrollment records, model weights and user datasets. Treat the VAOS data directory, backups, report exports, API tokens and integration secrets as sensitive.

## Supported versions

Security fixes target the latest 1.2.x release. Older releases should be upgraded after reviewing migration notes. The live installation may be on a different version; check `docs/PROJECT_STATUS.md` before assuming a source fix is active. Security fixes are not considered deployed until a release is reviewed and activated by the operator.

## Reporting a vulnerability

Do not publish an unpatched vulnerability, API token, camera/enrollment link, face data, private endpoint path or secret value in a public issue. Use [GitHub private vulnerability reporting](https://github.com/indipranisha/VAOS/security/advisories/new). If unavailable, contact the creator through https://prateekdubey.in and request a private reporting route. Include affected version, impact and minimal reproduction steps; remove personal or biometric data.

## Security design notes

- Administrator access should be kept on a trusted network or protected gateway; use HTTPS for remote access.
- API access is off by default. Use expiring, least-privilege tokens; rotate exposed credentials.
- MCP starts read-only and sensitive writes require the configured one-time administrator approval.
- External observability delivery is disabled by default and uses signed HTTPS JSON only. Do not place credentials in webhook paths or query strings.
- See [observability security and privacy](docs/observability/security.md), [API guide](docs/API.md), [MCP guide](docs/MCP.md) and [production setup](README.md).

## Staging credentials

The explicitly selected staging environment uses a fixed development password by design. Keep staging on loopback or an isolated trusted network. Never expose staging to the public internet or use its password in production. Production requires a unique configured secret of at least 32 characters.
