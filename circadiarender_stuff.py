from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import requests

#GET and POST urls
DEFAULT_SCHEDULE_URL = "https://circadianode.onrender.com/schedule"
DEFAULT_SETTINGS_URL = "https://circadianode.onrender.com/settings"

#load urls from env variables on render
#can also use fallback values just in case
def _get_url(env_name: str, default: str) -> str:
    value = os.getenv(env_name)

    if value is None or value.strip() == "":
        return default

    return value.strip()


def get_schedule_url() -> str:
    return _get_url("CIRCADIAN_SCHEDULE_URL", DEFAULT_SCHEDULE_URL)


def get_settings_url() -> str:
    return _get_url("CIRCADIAN_SETTINGS_URL", DEFAULT_SETTINGS_URL)

#parse json timestampstring into datetime
def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None

#same parsing procedure as other filesneed time zone and iso8601 format
def ensure_timezone_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.astimezone()

    return moment

#use seconds for overlapping time edge cases with web api
def to_iso8601_seconds(moment: datetime) -> str:
    return ensure_timezone_aware(moment).isoformat(timespec="seconds")

#check if another key or directly accessible 
def _unwrap_schedule_response(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RuntimeError("Schedule response must be a JSON object.")

    for wrapper_key in ("schedule", "settings", "data", "latest_schedule"):
        nested = data.get(wrapper_key)

        if isinstance(nested, dict):
            return dict(nested)

    return dict(data)

#GETs the current schedule stuff from the web API the app posts to
#follows normal GET protocol like we used with app
def get_schedule(timeout_seconds: int = 10) -> dict[str, Any]:
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

#need to have all values when we do the post payload so check for all the required values
def _required_value(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)

    if value is None or value == "":
        raise RuntimeError(f"Can't POST settings: missing  {key!r}.")

    return value

#need full payload for web api
def validate_settings_payload(payload: dict[str, Any]) -> None:

    _required_value(payload, "wake_time")
    _required_value(payload, "sleep_time")
    _required_value(payload, "temperature")
    _required_value(payload, "humidity")
    _required_value(payload, "alarm_time")

#when we get an alarm time it needs to follow iso8601 format and be in the right timezone
def _format_alarm_time_for_schedule_timezone(
    alarm_time: datetime | str,
    schedule: dict[str, Any],
) -> str:
    wake_time_raw = schedule.get("wake_time")
    wake_dt = parse_iso8601(str(wake_time_raw)) if wake_time_raw else None

    if isinstance(alarm_time, str):
        parsed_alarm = parse_iso8601(alarm_time)

        if parsed_alarm is None:
            raise RuntimeError(
                f"alarm_time must be ISO 8601 for string, got {alarm_time!r}."
            )

        alarm_dt = parsed_alarm
    else:
        alarm_dt = alarm_time

    alarm_dt = ensure_timezone_aware(alarm_dt)

    #conver tto same timezone as wake_time
    if wake_dt is not None and wake_dt.tzinfo is not None:
        alarm_dt = alarm_dt.astimezone(wake_dt.tzinfo)

    return to_iso8601_seconds(alarm_dt)

#builds the payload we need to push, keeps all values from GET the same except for alarm_time
def build_alarm_update_payload(
    schedule: dict[str, Any],
    alarm_time: datetime | str,
) -> dict[str, Any]:
    payload = dict(schedule)

    payload["alarm_time"] = _format_alarm_time_for_schedule_timezone(
        alarm_time=alarm_time,
        schedule=schedule,
    )

    validate_settings_payload(payload)
    return payload

#typical post procedure
#run the exdact same as app, timeout after 10 seconds
#make sure we have valid payload and the proper url
#can use status codecs to diagnose error
def post_settings(
    payload: dict[str, Any],
    timeout_seconds: int = 10,
) -> dict[str, Any]:
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
