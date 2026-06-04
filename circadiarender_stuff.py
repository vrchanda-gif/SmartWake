from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import requests


DEFAULT_SCHEDULE_URL = "https://circadianode.onrender.com/schedule"
DEFAULT_SETTINGS_URL = "https://circadianode.onrender.com/settings"


def _get_url(env_name: str, default: str) -> str:
    """
    Get a URL from an environment variable, or use the default.

    This lets Render override URLs later without changing code.
    """
    value = os.getenv(env_name)

    if value is None or value.strip() == "":
        return default

    return value.strip()


def get_schedule_url() -> str:
    """
    URL used by the worker to get the current user schedule/settings.
    """
    return _get_url("CIRCADIAN_SCHEDULE_URL", DEFAULT_SCHEDULE_URL)


def get_settings_url() -> str:
    """
    URL used by the worker to post the updated full payload.
    """
    return _get_url("CIRCADIAN_SETTINGS_URL", DEFAULT_SETTINGS_URL)


def parse_iso8601(value: str | None) -> datetime | None:
    """
    Parse an ISO 8601 datetime string.

    Handles strings like:
        2026-06-03T07:10:00-07:00

    Also handles Z-style UTC timestamps if they ever appear.
    """
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def ensure_timezone_aware(moment: datetime) -> datetime:
    """
    Make sure a datetime has timezone information.

    If it is already timezone-aware, keep it.
    If it is naive, attach the machine's local timezone.
    """
    if moment.tzinfo is None:
        return moment.astimezone()

    return moment


def to_iso8601_seconds(moment: datetime) -> str:
    """
    Convert datetime to ISO 8601 with seconds.

    Example:
        2026-06-03T07:10:00-07:00
    """
    return ensure_timezone_aware(moment).isoformat(timespec="seconds")


def _unwrap_schedule_response(data: Any) -> dict[str, Any]:
    """
    Accept either a direct schedule object or a wrapped response.

    Direct:
        {
            "wake_time": "...",
            "sleep_time": "...",
            "temperature": 70,
            "humidity": 45,
            "alarm_time": null
        }

    Wrapped:
        {
            "schedule": {
                ...
            }
        }
    """
    if not isinstance(data, dict):
        raise RuntimeError("Schedule response must be a JSON object.")

    for wrapper_key in ("schedule", "settings", "data", "latest_schedule"):
        nested = data.get(wrapper_key)

        if isinstance(nested, dict):
            return dict(nested)

    return dict(data)


def get_schedule(timeout_seconds: int = 10) -> dict[str, Any]:
    """
    GET the current schedule from circadianode.

    This gives the worker the full existing payload, including wake_time,
    sleep_time, temperature, humidity, and alarm_time if it already exists.
    """
    response = requests.get(
        get_schedule_url(),
        timeout=timeout_seconds,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            "Failed to GET schedule from circadianode. "
            f"HTTP {response.status_code}: {response.text}"
        ) from exc

    return _unwrap_schedule_response(response.json())


def _required_value(payload: dict[str, Any], key: str) -> Any:
    """
    Read a required payload value and fail clearly if it is missing.
    """
    value = payload.get(key)

    if value is None or value == "":
        raise RuntimeError(f"Cannot POST settings: missing required field {key!r}.")

    return value


def validate_settings_payload(payload: dict[str, Any]) -> None:
    """
    Make sure the payload has the fields circadianode /settings expects.

    This prevents another 422 caused by posting an incomplete body.
    """
    _required_value(payload, "wake_time")
    _required_value(payload, "sleep_time")
    _required_value(payload, "temperature")
    _required_value(payload, "humidity")
    _required_value(payload, "alarm_time")


def _format_alarm_time_for_schedule_timezone(
    alarm_time: datetime | str,
    schedule: dict[str, Any],
) -> str:
    """
    Format alarm_time as an ISO 8601 string.

    If wake_time includes a timezone offset, convert alarm_time into that same
    timezone before posting. This helps produce strings like:

        2026-06-03T07:10:00-07:00

    instead of an equivalent UTC string like:

        2026-06-03T14:10:00+00:00
    """
    wake_time_raw = schedule.get("wake_time")
    wake_dt = parse_iso8601(str(wake_time_raw)) if wake_time_raw else None

    if isinstance(alarm_time, str):
        parsed_alarm = parse_iso8601(alarm_time)

        if parsed_alarm is None:
            raise RuntimeError(
                f"alarm_time must be ISO 8601 if passed as a string, got {alarm_time!r}."
            )

        alarm_dt = parsed_alarm
    else:
        alarm_dt = alarm_time

    alarm_dt = ensure_timezone_aware(alarm_dt)

    if wake_dt is not None and wake_dt.tzinfo is not None:
        alarm_dt = alarm_dt.astimezone(wake_dt.tzinfo)

    return to_iso8601_seconds(alarm_dt)


def build_alarm_update_payload(
    schedule: dict[str, Any],
    alarm_time: datetime | str,
) -> dict[str, Any]:
    """
    Preserve the full schedule payload and update only alarm_time.

    This is the key function that prevents 422 errors from circadianode /settings.
    """
    payload = dict(schedule)

    payload["alarm_time"] = _format_alarm_time_for_schedule_timezone(
        alarm_time=alarm_time,
        schedule=schedule,
    )

    validate_settings_payload(payload)

    return payload


def post_settings(
    payload: dict[str, Any],
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """
    POST the full updated settings payload to circadianode /settings.
    """
    validate_settings_payload(payload)

    response = requests.post(
        get_settings_url(),
        json=payload,
        timeout=timeout_seconds,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            "Failed to POST settings to circadianode. "
            f"HTTP {response.status_code}: {response.text}. "
            f"Payload sent: {payload}"
        ) from exc

    try:
        response_body = response.json()
    except ValueError:
        response_body = {"raw_body": response.text}

    return {
        "status_code": response.status_code,
        "payload_sent": payload,
        "response_body": response_body,
    }