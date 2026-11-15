"""Validated, in-process evaluation for VAOS object tracking rules.

This module consumes detections and short-lived tracker keys supplied by the
existing camera pipeline. It does not create a detector or claim identity.
"""

from __future__ import annotations

import math
from typing import Any


RULE_TYPES = {"count_in_region", "dwell_in_region"}


def _finite_number(value: Any, field: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number between {low:g} and {high:g}.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a number between {low:g} and {high:g}.") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{field} must be a number between {low:g} and {high:g}.")
    return number


def _integer(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a whole number between {low} and {high}.")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a whole number between {low} and {high}.") from exc
    if str(value).strip() not in {str(number), f"{number}.0"} or not low <= number <= high:
        raise ValueError(f"{field} must be a whole number between {low} and {high}.")
    return number


def validate_rule(payload: Any) -> dict[str, Any]:
    """Normalize and validate one user-provided rule without touching storage."""
    if not isinstance(payload, dict):
        raise ValueError("Send the tracking rule as a JSON object.")
    name = str(payload.get("name", "")).strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("Rule name must contain between 1 and 80 characters.")
    rule_type = str(payload.get("rule_type", "")).strip().casefold()
    if rule_type not in RULE_TYPES:
        raise ValueError("Rule type must be count_in_region or dwell_in_region.")
    class_name = str(payload.get("class_name", "")).strip().casefold()
    if not 1 <= len(class_name) <= 80:
        raise ValueError("Choose an object label between 1 and 80 characters.")
    camera_token = payload.get("camera_token")
    if camera_token is None or camera_token == "" or camera_token == "all":
        camera_token = None
    elif not isinstance(camera_token, str) or not 8 <= len(camera_token) <= 160 or any(ord(char) < 32 for char in camera_token):
        raise ValueError("Camera selection is invalid.")

    region_payload = payload.get("region") or {"x": 0, "y": 0, "width": 1, "height": 1}
    if not isinstance(region_payload, dict):
        raise ValueError("Region must be a JSON object with x, y, width, and height.")
    x = _finite_number(region_payload.get("x", 0), "Region left", 0, 1)
    y = _finite_number(region_payload.get("y", 0), "Region top", 0, 1)
    width = _finite_number(region_payload.get("width", 1), "Region width", 0.01, 1)
    height = _finite_number(region_payload.get("height", 1), "Region height", 0.01, 1)
    if x + width > 1.000001 or y + height > 1.000001:
        raise ValueError("The region must fit inside the normalized frame (0–100%).")

    enabled_value = payload.get("enabled", True)
    if not isinstance(enabled_value, bool):
        raise ValueError("Enabled must be true or false.")
    result = {
        "name": name,
        "enabled": enabled_value,
        "rule_type": rule_type,
        "class_name": class_name,
        "min_confidence": _finite_number(payload.get("min_confidence", 0.35), "Minimum confidence", 0.05, 0.99),
        "threshold_count": _integer(payload.get("threshold_count", 1), "Object count", 1, 100),
        "min_consecutive_frames": _integer(payload.get("min_consecutive_frames", 3), "Confirmation frames", 1, 120),
        "cooldown_seconds": _integer(payload.get("cooldown_seconds", 60), "Cooldown", 5, 3600),
        "camera_token": camera_token,
        "region": {"x": x, "y": y, "width": width, "height": height},
    }
    if rule_type == "dwell_in_region":
        dwell_seconds = payload.get("dwell_seconds")
        result["dwell_seconds"] = _finite_number(10 if dwell_seconds is None else dwell_seconds, "Dwell time", 1, 3600)
    else:
        result["dwell_seconds"] = None
    return result


def _inside_region(obj: dict[str, Any], frame_size: tuple[int, int], region: dict[str, float]) -> bool:
    width, height = frame_size
    if width <= 0 or height <= 0:
        return False
    box = obj.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    try:
        x1, y1, x2, y2 = (float(value) for value in box)
    except (TypeError, ValueError, OverflowError):
        return False
    center_x = ((x1 + x2) / 2) / width
    center_y = ((y1 + y2) / 2) / height
    return region["x"] <= center_x <= region["x"] + region["width"] and region["y"] <= center_y <= region["y"] + region["height"]


def evaluate_rules(
    *,
    rules: list[dict[str, Any]],
    camera_token: str,
    objects: list[dict[str, Any]],
    frame_size: tuple[int, int],
    state: dict[tuple[Any, ...], dict[str, Any]],
    now: float,
    tracking_enabled: bool,
) -> list[dict[str, Any]]:
    """Evaluate active rules and return alert metadata; never stores images."""
    emitted: list[dict[str, Any]] = []
    for key in [key for key, item in state.items() if now - float(item.get("last_seen", now)) > 10.0]:
        state.pop(key, None)
    for rule in rules:
        if not rule.get("enabled", True) or rule.get("camera_token") not in {None, camera_token}:
            continue
        rule_id = int(rule["id"])
        rule_type = rule.get("rule_type")
        region = rule.get("region") or {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
        hits = []
        for obj in objects:
            try:
                score = float(obj.get("score", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if (str(obj.get("label", "")).casefold() == rule["class_name"]
                    and score >= float(rule["min_confidence"])
                    and _inside_region(obj, frame_size, region)):
                hits.append(obj)

        if rule_type == "count_in_region":
            key = ("count", rule_id, camera_token)
            item = state.setdefault(key, {"consecutive": 0, "last_event": float("-inf"), "last_seen": now})
            item["last_seen"] = now
            item["consecutive"] = item["consecutive"] + 1 if len(hits) >= int(rule["threshold_count"]) else 0
            if item["consecutive"] >= int(rule["min_consecutive_frames"]) and now - item["last_event"] >= int(rule["cooldown_seconds"]):
                item["last_event"] = now
                emitted.append({
                    "rule_id": rule_id, "rule_name": str(rule["name"]), "rule_type": rule_type,
                    "class_name": str(rule["class_name"]), "object_count": len(hits),
                    "message": f"{len(hits)} {rule['class_name']} detected in the configured region.",
                })
            continue

        if rule_type != "dwell_in_region":
            continue
        if not tracking_enabled:
            prefix = ("dwell", rule_id, camera_token)
            for key in [key for key in state if key[:3] == prefix]:
                state.pop(key, None)
            continue
        visible_keys: set[tuple[Any, ...]] = set()
        for obj in hits:
            track_key = obj.get("_tracking_key")
            if not track_key:
                continue
            key = ("dwell", rule_id, camera_token, str(track_key))
            visible_keys.add(key)
            item = state.setdefault(key, {"first_seen": now, "consecutive": 0, "last_event": float("-inf"), "last_seen": now})
            item["last_seen"] = now
            item["consecutive"] += 1
            if (item["consecutive"] >= int(rule["min_consecutive_frames"])
                    and now - item["first_seen"] >= float(rule["dwell_seconds"])
                    and now - item["last_event"] >= int(rule["cooldown_seconds"])):
                item["last_event"] = now
                emitted.append({
                    "rule_id": rule_id, "rule_name": str(rule["name"]), "rule_type": rule_type,
                    "class_name": str(rule["class_name"]), "object_count": 1,
                    "message": f"A tracked {rule['class_name']} remained in the configured region.",
                })
        prefix = ("dwell", rule_id, camera_token)
        for key in [key for key in state if key[:3] == prefix and key not in visible_keys]:
            state.pop(key, None)
    return emitted
