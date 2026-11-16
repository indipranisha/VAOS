#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$repo_root/compose.production.yaml"
temporary_root="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/vaos-container-smoke.XXXXXX")"
env_file="$temporary_root/production.env"
project_name="vaos-ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
project_name="$(printf '%s' "$project_name" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"

umask 077
admin_password="$(python -c 'import secrets; print(secrets.token_urlsafe(36))')"
internal_token="$(python -c 'import secrets; print(secrets.token_urlsafe(36))')"
cat >"$env_file" <<EOF
VAOS_ADMIN_DOMAIN=admin.vaos-ci.invalid
VAOS_CAMERA_DOMAIN=camera.vaos-ci.invalid
VAOS_ADMIN_PASSWORD=$admin_password
VAOS_INTERNAL_STATUS_TOKEN=$internal_token
VAOS_INSTALL_OBJECT_DETECTION=true
VAOS_TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
OBJECT_DETECTOR_DEVICE=cpu
EOF

dc() {
  docker compose --project-name "$project_name" --env-file "$env_file" -f "$compose_file" "$@"
}

cleanup() {
  local result=$?
  if [[ $result -ne 0 ]]; then
    dc logs --no-color vaos-admin vaos-camera || true
  fi
  dc down --volumes --remove-orphans || true
  rm -rf -- "$temporary_root"
  exit "$result"
}
trap cleanup EXIT

echo "[container-smoke] project=$project_name"
docker --version
docker compose version
dc config --quiet
echo "[container-smoke] building the production CPU detector images"
dc build --pull vaos-admin vaos-camera
echo "[container-smoke] starting isolated admin and camera services"
dc up --detach --no-build vaos-admin vaos-camera

healthy=false
echo "[container-smoke] waiting for both service health checks"
for attempt in $(seq 1 120); do
  all_healthy=true
  for service in vaos-admin vaos-camera; do
    container_id="$(dc ps --quiet "$service")"
    if [[ -z "$container_id" ]]; then
      all_healthy=false
      continue
    fi
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
    case "$health" in
      healthy) ;;
      unhealthy|exited|dead)
        echo "$service entered terminal state: $health" >&2
        exit 1
        ;;
      *) all_healthy=false ;;
    esac
  done
  if [[ "$all_healthy" == true ]]; then
    healthy=true
    break
  fi
  sleep 3
done

if [[ "$healthy" != true ]]; then
  echo "Production admin/camera containers did not both become healthy within six minutes." >&2
  exit 1
fi

admin_id="$(dc ps --quiet vaos-admin)"
camera_id="$(dc ps --quiet vaos-camera)"
echo "[container-smoke] checking CPU dependencies and internal service authentication"
docker exec "$admin_id" python -c 'import cv2,torch,ultralytics; assert cv2.__version__ and torch.__version__ and ultralytics.__version__; print("CPU detector runtime imports passed:", cv2.__version__, torch.__version__, ultralytics.__version__)'
docker exec "$admin_id" python -c 'import json,urllib.request; r=json.load(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5)); assert r.get("status")=="ok" and r.get("app")=="vaos-admin", r'
docker exec "$camera_id" python -c 'import json,urllib.request; r=json.load(urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=5)); assert r.get("status")=="ok" and r.get("app")=="vaos-camera", r'
docker exec "$admin_id" python -c 'import json,os,urllib.request; req=urllib.request.Request("http://vaos-camera:8001/internal/model-status", headers={"X-VAOS-Internal-Token":os.environ["VAOS_INTERNAL_STATUS_TOKEN"]}); r=json.load(urllib.request.urlopen(req, timeout=10)); assert isinstance(r,dict), type(r)'

echo "[container-smoke] auditing the built image's installed Python packages against OSV"
docker exec "$admin_id" python -m venv /tmp/vaos-security-audit
docker exec "$admin_id" /tmp/vaos-security-audit/bin/python -m pip install --disable-pip-version-check --no-cache-dir pip-audit==2.10.1
docker exec "$admin_id" /tmp/vaos-security-audit/bin/python -m pip_audit --path /usr/local/lib/python3.12/site-packages --vulnerability-service osv --progress-spinner off
docker exec "$admin_id" rm -rf /tmp/vaos-security-audit

echo "Production CPU detector image started both isolated services; imports, admin/camera health and authenticated internal model-status checks passed."
