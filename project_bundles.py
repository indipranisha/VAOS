"""Safe, configuration-only VAOS project bundles.

Bundles are ZIP containers with two fixed JSON members. They never contain
weights, images, credentials, camera capabilities, or executable content.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import stat
import zipfile
import zlib
from typing import Any


FORMAT_NAME = "vaos-project"
FORMAT_VERSION = 1
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_MEMBER_BYTES = 1 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_COMPRESSION_RATIO = 80
MAX_REFERENCES = 100
MEMBERS = {"manifest.json", "project.json"}

ENGINE_BOOLEAN_SETTINGS = {
    "face_matching_enabled", "object_detection_enabled", "object_tracking_enabled",
}
ENGINE_NUMERIC_SETTINGS = {
    "match_threshold": (0.20, 0.85),
    "object_confidence": (0.05, 0.95),
}
ENGINE_INTEGER_SETTINGS = {
    "event_cooldown_seconds": {"min": 15, "max": 3600},
    "object_image_size": {"values": {320, 416, 512, 640, 768}},
    "camera_frame_interval_ms": {"values": {250, 500, 750, 1000, 1500}},
    "camera_max_dimension": {"values": {480, 640, 800, 960, 1280}},
    "training_status_refresh_seconds": {"min": 1, "max": 10},
}
ENGINE_SETTING_KEYS = ENGINE_BOOLEAN_SETTINGS | set(ENGINE_NUMERIC_SETTINGS) | set(ENGINE_INTEGER_SETTINGS)


class BundleError(ValueError):
    """The archive is not a supported, safe VAOS project bundle."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _text(value: Any, label: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise BundleError(f"{label} must be text.")
    normalized = value.strip()
    if required and not normalized:
        raise BundleError(f"{label} is required.")
    if len(normalized) > limit or any(ord(char) < 32 and char not in "\t\n\r" for char in normalized):
        raise BundleError(f"{label} is too long or contains unsupported control characters.")
    return normalized


def _references(value: Any, label: str, *, models: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_REFERENCES:
        raise BundleError(f"{label} must be a list of no more than {MAX_REFERENCES} references.")
    normalized = []
    allowed = {"id", "name", "kind"} if models else {"id", "name", "format", "train_count", "val_count", "class_count"}
    for item in value:
        if not isinstance(item, dict) or set(item) != allowed:
            raise BundleError(f"Each {label[:-1].lower()} reference must use the documented fields only.")
        entry = {
            "id": _text(item.get("id"), f"{label} ID", 160, required=True),
            "name": _text(item.get("name"), f"{label} name", 160, required=True),
        }
        if models:
            entry["kind"] = _text(item.get("kind"), "Model kind", 40, required=True)
        else:
            entry["format"] = _text(item.get("format"), "Dataset format", 40)
            for key in ("train_count", "val_count", "class_count"):
                count = item.get(key)
                if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1_000_000_000:
                    raise BundleError(f"Dataset {key} must be a non-negative whole number.")
                entry[key] = count
        normalized.append(entry)
    identifiers = [entry["id"].casefold() for entry in normalized]
    if len(identifiers) != len(set(identifiers)):
        raise BundleError(f"{label} contains duplicate identifiers.")
    return normalized


def validate_project(project: Any) -> dict[str, Any]:
    """Strictly validate the shareable fields; unknown keys are rejected."""
    allowed = {"name", "description", "engine_settings", "dataset_refs", "model_refs"}
    if not isinstance(project, dict) or set(project) != allowed:
        raise BundleError("The project configuration has missing or unsupported fields.")
    settings = project.get("engine_settings")
    if not isinstance(settings, dict) or not settings or not set(settings).issubset(ENGINE_SETTING_KEYS):
        raise BundleError("The bundle contains unsupported or empty engine settings.")
    safe_settings: dict[str, Any] = {}
    for key, value in settings.items():
        if key in ENGINE_BOOLEAN_SETTINGS:
            if not isinstance(value, bool):
                raise BundleError(f"{key} must be true or false.")
            safe_settings[key] = value
        elif key in ENGINE_NUMERIC_SETTINGS:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise BundleError(f"{key} must be a finite number.")
            low, high = ENGINE_NUMERIC_SETTINGS[key]
            if not low <= float(value) <= high:
                raise BundleError(f"{key} must be between {low} and {high}.")
            safe_settings[key] = float(value)
        else:
            bounds = ENGINE_INTEGER_SETTINGS[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise BundleError(f"{key} must be a whole number.")
            if "values" in bounds:
                if value not in bounds["values"]:
                    raise BundleError(f"{key} is not a supported option.")
            elif not bounds["min"] <= value <= bounds["max"]:
                raise BundleError(f"{key} is outside the supported range.")
            safe_settings[key] = value
    return {
        "name": _text(project.get("name"), "Project name", 100, required=True),
        "description": _text(project.get("description"), "Project description", 1000),
        "engine_settings": safe_settings,
        "dataset_refs": _references(project.get("dataset_refs"), "Dataset references", models=False),
        "model_refs": _references(project.get("model_refs"), "Model references", models=True),
    }


def build_bundle(project: Any, *, app_version: str, created_at: str) -> tuple[bytes, dict[str, Any]]:
    """Build a deterministic two-member archive and return bytes + manifest."""
    safe_project = validate_project(project)
    project_bytes = _json_bytes(safe_project)
    manifest = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "created_at": _text(created_at, "Creation time", 64, required=True),
        "producer_version": _text(app_version, "VAOS version", 40, required=True),
        "project_sha256": hashlib.sha256(project_bytes).hexdigest(),
    }
    manifest_bytes = _json_bytes(manifest)
    output = io.BytesIO()
    # Configuration archives are tiny; storing them uncompressed avoids
    # creating highly compressible input that our bomb-ratio guard rejects.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        archive.writestr("project.json", project_bytes)
    data = output.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise BundleError("The configuration bundle is larger than VAOS allows.")
    return data, manifest


def inspect_bundle(data: bytes) -> dict[str, Any]:
    """Read and validate without extracting or writing any archive member."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_ARCHIVE_BYTES:
        raise BundleError("Choose a non-empty VAOS project bundle no larger than 2 MB.")
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            infos = archive.infolist()
            names = [entry.filename for entry in infos]
            if len(infos) != 2 or set(names) != MEMBERS or len({name.casefold() for name in names}) != len(names):
                raise BundleError("The bundle must contain only manifest.json and project.json, with no duplicate entries.")
            total_size = 0
            for entry in infos:
                if entry.is_dir() or entry.filename not in MEMBERS or "\\" in entry.filename or "/" in entry.filename:
                    raise BundleError("The bundle contains an unexpected or unsafe archive path.")
                mode = (entry.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise BundleError("Symbolic links are not allowed in a VAOS project bundle.")
                if entry.flag_bits & 0x1 or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise BundleError("Encrypted or unsupported compressed archive members are not allowed.")
                if entry.file_size > MAX_MEMBER_BYTES:
                    raise BundleError("A project bundle member is larger than VAOS allows.")
                total_size += entry.file_size
                ratio = entry.file_size / max(1, entry.compress_size)
                if ratio > MAX_COMPRESSION_RATIO:
                    raise BundleError("The bundle has an unsafe compression ratio.")
            if total_size > MAX_TOTAL_BYTES:
                raise BundleError("The uncompressed bundle is larger than VAOS allows.")
            def bounded_read(member_name: str) -> bytes:
                with archive.open(member_name, "r") as member:
                    payload = member.read(MAX_MEMBER_BYTES + 1)
                    if len(payload) > MAX_MEMBER_BYTES:
                        raise BundleError("A project bundle member expands beyond the allowed size.")
                    return payload

            raw_manifest = bounded_read("manifest.json")
            raw_project = bounded_read("project.json")
    except BundleError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, EOFError, zlib.error, NotImplementedError) as exc:
        raise BundleError("The file is not a readable VAOS project bundle.") from exc
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
        raw_project_value = json.loads(raw_project.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError("The bundle manifest or project configuration is not valid UTF-8 JSON.") from exc
    manifest_fields = {"format", "format_version", "created_at", "producer_version", "project_sha256"}
    if not isinstance(manifest, dict) or set(manifest) != manifest_fields:
        raise BundleError("The bundle manifest has missing or unsupported fields.")
    if manifest.get("format") != FORMAT_NAME or type(manifest.get("format_version")) is not int or manifest.get("format_version") != FORMAT_VERSION:
        raise BundleError("This VAOS project bundle format is not supported by this version.")
    if not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("project_sha256", ""))):
        raise BundleError("The project checksum in the manifest is invalid.")
    if not hashlib.sha256(raw_project).hexdigest() == manifest["project_sha256"]:
        raise BundleError("The project configuration checksum does not match the manifest.")
    manifest = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "created_at": _text(manifest.get("created_at"), "Creation time", 64, required=True),
        "producer_version": _text(manifest.get("producer_version"), "VAOS version", 40, required=True),
        "project_sha256": manifest["project_sha256"],
    }
    return {"manifest": manifest, "project": validate_project(raw_project_value), "archive_sha256": hashlib.sha256(data).hexdigest()}


# VAOS envelope v1: fixed parameters prevent untrusted KDF resource requests.
ENVELOPE_MAGIC = b"VAOS-SEALED-1\n"
MAX_ENCRYPTED_BYTES = 3 * 1024 * 1024


def validate_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str) or not re.fullmatch(r"[A-Za-z0-9]{8}", passphrase):
        raise BundleError("Enter exactly 8 alphanumeric characters (A–Z, a–z, 0–9).")


def _bundle_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(passphrase.encode("ascii"))


def encrypt_bundle(data: bytes, passphrase: str) -> bytes:
    """Seal the validated ZIP in a versioned, base64-encoded AES-256-GCM envelope."""
    import base64
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    validate_passphrase(passphrase)
    inspect_bundle(data)
    salt, nonce = os.urandom(16), os.urandom(12)
    ciphertext = AESGCM(_bundle_key(passphrase, salt)).encrypt(nonce, data, ENVELOPE_MAGIC)
    return ENVELOPE_MAGIC + base64.b64encode(salt + nonce + ciphertext)


def decrypt_bundle(data: bytes, passphrase: str) -> bytes:
    """Authenticate before parsing; never fall back to an unencrypted upload."""
    import base64
    import binascii
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    validate_passphrase(passphrase)
    if len(data) > MAX_ENCRYPTED_BYTES or not data.startswith(ENVELOPE_MAGIC):
        raise BundleError("Choose an encrypted VAOS bundle exported by this version. Legacy ZIP uploads are not supported.")
    try:
        raw = base64.b64decode(data[len(ENVELOPE_MAGIC):], validate=True)
        if len(raw) < 45 or len(raw) > MAX_ARCHIVE_BYTES + 44:
            raise ValueError("Invalid envelope size")
        plaintext = AESGCM(_bundle_key(passphrase, raw[:16])).decrypt(raw[16:28], raw[28:], ENVELOPE_MAGIC)
    except (InvalidTag, ValueError, binascii.Error) as exc:
        raise BundleError("Could not unlock bundle: incorrect passphrase or damaged file.") from exc
    inspect_bundle(plaintext)
    return plaintext
