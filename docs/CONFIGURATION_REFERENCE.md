# VAOS configuration and environment reference

This is the source-of-truth guide to environment variables read by the application, included launchers, production Compose files and optional CLI/MCP processes. Settings stored in the VAOS database are managed in the interface and are not environment variables. Never commit real passwords, tokens, private keys or `production.env`.

## Application and local service variables

| Variable | Default | Used by / meaning | Handling |
|---|---|---|---|
| `VAOS_ENV` | `development` | `main.py`; only the exact value `production` enables production checks. | Set `production` in the protected production container environment. |
| `VAOS_ADMIN_PASSWORD` | Generated locally if absent | Admin Basic Auth password. Production requires at least 32 characters and rejects `REPLACE_` placeholders; configured development password requires 20+. | Secret. Prefer a unique random value. In local development, protect the generated `data/admin_password.txt`. |
| `VAOS_DATA_DIR` | `<application>/data` | Root for SQLite database, images, logs, ML runs and other persistent state. Docker sets `/var/lib/vaos`. | Persistent, private writable directory. Back it up; it can contain biometric data. |
| `MODEL_DIR` | `<VAOS_DATA_DIR>/models` | Face/object model cache location. | Persistent writable directory; plan disk and backup separately if it is outside the data root. |
| `VAOS_DATASETS_DIR` | Platform-native default | Root for local source/prepared datasets. | Persistent writable directory; review exact paths before copying or purging. |
| `YOLO_CONFIG_DIR` | Ultralytics default if omitted | Ultralytics configuration/cache directory; Compose sets `/var/lib/vaos/ultralytics`. | Writable path; keep it on persistent storage if cache persistence is desired. |
| `VAOS_CAMERA_INTERNAL_URL` | `http://127.0.0.1:8001` | Admin-to-camera private health/model-status requests. Compose sets `http://vaos-camera:8001`. | Keep private to the VAOS network. |
| `VAOS_PUBLIC_CAMERA_URL` | Empty | Public camera-site origin used to create remote camera links. In production it must be an HTTPS origin with no credentials, path, query or fragment. | Public origin only. Never point this at the admin app. |
| `VAOS_INTERNAL_STATUS_TOKEN` | Empty outside launcher provisioning | Shared secret protecting camera-service internal model status. Production requires a unique value of at least 32 characters. Windows launcher creates a per-run random token. | Secret; different from the admin password. |
| `VAOS_TRUSTED_PROXY_CIDRS` | Empty | Comma-separated IP/CIDR ranges allowed to supply trusted forwarding headers for request attribution and HTTPS decisions. | Only list the actual reverse-proxy peer addresses. Never use `0.0.0.0/0` or `::/0`. |
| `OBJECT_DETECTOR_DEVICE` | `auto` | Object inference backend choice. Supported values include `auto`, `cpu`, or a runtime-supported CUDA/MPS device selection handled by `runtime_compat.py`. | Invalid/unavailable accelerators fall back or report unavailable. Verify actual model status. Training device is selected separately in ML Training. |
| `MATCH_THRESHOLD` | `0.42` | Default face-match threshold when initializing engine settings. | Numeric threshold; evaluate on a consented representative dataset. Not a probability. |
| `EVENT_COOLDOWN_SECONDS` | `120` | Default cooldown for repeated same-person/camera events. | Integer seconds; UI/API validation also applies to saved configuration. |
| `VAOS_HEALTH_PORT` | `8000` | Docker image health-check port. Compose overrides to `8000` for admin and `8001` for camera. | Container health check only; normally leave managed by Compose. |

The Windows launcher sets `CAMERA_SERVICE_IMPORT=1` when importing common code in the camera worker so that the worker does not initialize or expose the admin password. Do not set this as an operator workaround. Other process variables such as `PATH`, `SystemRoot`, `LOCALAPPDATA`, `XDG_STATE_HOME`, `CUDA_VISIBLE_DEVICES`, and Python runtime settings are owned by the operating system or PyTorch rather than VAOS; configure those using their respective documentation.

## Production Compose variables

These are read/interpolated by `compose.production.yaml` and `compose.production.gpu.yaml`, not by the Windows launcher.

| Variable | Default / requirement | Meaning |
|---|---|---|
| `VAOS_ADMIN_DOMAIN` | Required | DNS hostname for private administrator site; Compose/Caddy require it. Protect it with VPN/identity/network controls. |
| `VAOS_CAMERA_DOMAIN` | Required | DNS hostname for public HTTPS camera site. |
| `VAOS_ADMIN_PASSWORD` | Required, unique, 32+ characters | Production admin password. |
| `VAOS_INTERNAL_STATUS_TOKEN` | Required, separate unique 32+ character secret | Internal admin-to-camera status authorization. |
| `VAOS_INSTALL_OBJECT_DETECTION` | `true` | Build argument enabling optional Torch/Ultralytics install in the image. `false` makes image smaller and leaves object detection/training unavailable. |
| `VAOS_TORCH_INDEX_URL` | CPU wheel index in base Compose; required by GPU override | PyTorch wheel repository. For GPU, select a compatible CUDA wheel index using the official PyTorch selector and the server driver; use same value for both services. |
| `OBJECT_DETECTOR_DEVICE` | `auto` | Runtime inference selection. Set `cpu` to force CPU inference. |

`production.env.example` is a template. Copy it to an untracked `production.env`, replace every placeholder, restrict file permissions, and use `--env-file production.env`. Do not store the real file in Git, screenshots or issue attachments. Example one-time secret generation:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Run the base CPU stack:

```sh
docker compose --env-file production.env -f compose.production.yaml up -d --build
```

For NVIDIA Docker, configure `VAOS_TORCH_INDEX_URL`, install the compatible NVIDIA driver and Container Toolkit, and add `-f compose.production.gpu.yaml` to Compose commands. A successful image build is not GPU validation.

## API CLI variables

| Variable | Default | Meaning and safe use |
|---|---|---|
| `VAOS_URL` | `http://127.0.0.1:8000` | VAOS origin used by the optional `vaos` CLI. Remote non-loopback connections must use HTTPS. |
| `VAOS_API_TOKEN` | Empty | Bearer token for the CLI or MCP bridge. Takes precedence over username/password. Store in a secret store, scope narrowly, set expiry and revoke when unused. |
| `VAOS_USERNAME` | `admin` | CLI Basic Auth username when no token is supplied. |
| `VAOS_PASSWORD` | Prompted interactively if absent | CLI Basic Auth password; avoid setting it in shell history or shared process listings. |

## Optional MCP bridge variables

| Variable | Default | Meaning |
|---|---|---|
| `VAOS_BASE_URL` | `http://127.0.0.1:8000` | Origin for `mcp_server.py`. Remote use must follow the MCP guide and use HTTPS. |
| `VAOS_API_TOKEN` | Empty | Scoped VAOS API token used by MCP; secret. |
| `VAOS_MCP_TELEMETRY_DB` | Platform state directory | Optional local SQLite path for MCP invocation telemetry. Otherwise Windows uses LocalAppData and POSIX uses XDG state/home defaults. |

The MCP bridge is a local stdio process. Do not expose it as a public server. Grant only the minimum API groups and approvals required by the agent.

## Optional security-event integration variables

| Variable | Default | Meaning |
|---|---|---|
| `VAOS_SIEM_CA_BUNDLE` | System trust roots | Optional absolute path to a PEM CA bundle used to extend trusted roots for SIEM TLS. The file is limited to 4 MB; TLS hostname/certificate verification remains required. |
| `VAOS_SIEM_HMAC_KEY` | Not set | Example environment reference for a signed HTTPS delivery key; configure at least 32 UTF-8 bytes. The Settings form stores the variable name, not its value. |
| `VAOS_OTLP_BEARER_TOKEN` | Not set | Example environment reference for OTLP/HTTP bearer authentication. |
| Other secret-reference names | Not set | Settings accepts a variable name matching uppercase letters/digits/underscore, beginning with a letter and at least three characters. The selected transport decides whether the value is a secret; provide it to the service environment before enabling delivery. |

Do not type secret values into the Settings reference field. External event delivery is opt-in; check the destination, TLS, redaction and event scope before enabling it.

## Test/CI-only variables

| Variable | Purpose |
|---|---|
| `VAOS_UI_ARTIFACT_DIR` | Directory for Playwright CI screenshots. |
| `VAOS_INTERNAL_STATUS_TOKEN` | Used by isolated CI container tests with a generated secret. |
| `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `RUNNER_TEMP`, `TMPDIR` | GitHub Actions/test runner temporary-resource values; not VAOS production configuration. |

## Configuration precedence and restart notes

- Environment variables exist at process start. Changing them does not reliably change an already running service; restart the owning process after editing configuration.
- Compose substitutes variables from the shell or the file passed through `--env-file`. The container's explicit `environment` block determines what reaches the service.
- UI-managed settings are stored in SQLite. They are not automatically overridden by every environment variable after initialization.
- The Windows launcher sets internal values for its own child services. Do not copy generated internal tokens into source control or use one deployment's secrets in another.
- Keep development and production data roots separate. Never point two unrelated VAOS instances at the same writable state unless their coordination and backup model is explicitly supported.

## Safe secret rotation

1. Create a new unique value in the secret store/environment file.
2. Restart only the affected services in a controlled maintenance window.
3. Confirm health and test the authenticated path.
4. Revoke old API/MCP tokens in Settings and remove old values from secret stores/backups according to policy.
5. Never print actual secret values into logs or share them in support requests.

## HTTP request-size safeguards

The application enforces streaming request-body limits before a multipart parser or endpoint can spool/allocate the complete body. The limits apply even when `Content-Length` is absent or incorrect:

| Request | Maximum body | Why |
|---|---:|---|
| Ordinary API write | 16 MiB | Bounds JSON/form writes by default. |
| Camera frame, video-review frame, one administrator enrollment photo | 7 MiB | Allows the existing 6 MiB image limit plus request framing. |
| Public multi-photo enrollment | 33 MiB | Allows the existing 30 MiB total photo limit plus multipart fields/framing. |
| Project-bundle import | 3 MiB | Allows the existing 2 MiB bundle limit plus multipart framing. |
| Model-source upload | 2 GiB + 1 MiB | Preserves the existing 2 GiB model-file limit plus multipart framing. |

These are source-controlled safety limits, not environment variables. An endpoint's own validation remains in place and can impose a smaller limit. There is no operator setting that raises them; changing a ceiling requires a code review and corresponding tests.
