from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials

from database import load_google_token, save_google_token
from smartwake_logic import HeartRateSample, ensure_timezone_aware


DEFAULT_GOOGLE_HEALTH_BASE_URL = "https://health.googleapis.com/v4"
DEFAULT_HEART_RATE_PARENT = "users/me/dataTypes/heart-rate"


def _get_env(name: str, default: str) -> str:
    value = os.getenv(name)

    if value is None or value.strip() == "":
        return default

    return value.strip()


def get_google_health_base_url() -> str:
    return _get_env("GOOGLE_HEALTH_BASE_URL", DEFAULT_GOOGLE_HEALTH_BASE_URL)


def get_heart_rate_parent() -> str:
    return _get_env("GOOGLE_HEALTH_HEART_RATE_PARENT", DEFAULT_HEART_RATE_PARENT)


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    return ensure_timezone_aware(parsed)


def to_utc(moment: datetime) -> datetime:
    return ensure_timezone_aware(moment).astimezone(timezone.utc)


def to_iso8601_utc_z(moment: datetime) -> str:
    return to_utc(moment).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_credentials_from_database() -> Credentials:
    token_info = load_google_token()

    if not token_info:
        raise RuntimeError(
            "Google OAuth is not connected yet. "
            "Visit /auth/google on the SmartWake API first."
        )

    credentials = Credentials.from_authorized_user_info(token_info)

    return credentials


def get_credentials() -> Credentials:
    """
    Load Google OAuth credentials from Postgres.

    If the access token is expired, refresh it using the saved refresh token
    and save the updated token JSON back to Postgres.
    """
    credentials = _load_credentials_from_database()

    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
        save_google_token(credentials.to_json())
        return credentials

    if not credentials.refresh_token:
        raise RuntimeError(
            "Saved Google credentials do not include a refresh token. "
            "Restart Google authorization from /auth/google and make sure "
            "offline access is requested."
        )

    credentials.refresh(GoogleAuthRequest())
    save_google_token(credentials.to_json())

    return credentials


def _auth_headers() -> dict[str, str]:
    credentials = get_credentials()

    return {
        "Authorization": f"Bearer {credentials.token}",
        "Accept": "application/json",
    }


def _heart_rate_filter(start_time: datetime, end_time: datetime) -> str:
    start = to_iso8601_utc_z(start_time)
    end = to_iso8601_utc_z(end_time)

    return (
        f'heart_rate.sample_time.physical_time >= "{start}" '
        f'AND heart_rate.sample_time.physical_time < "{end}"'
    )


def _request_heart_rate_page(
    start_time: datetime,
    end_time: datetime,
    page_token: str | None = None,
) -> dict[str, Any]:
    url = (
        f"{get_google_health_base_url()}/"
        f"{get_heart_rate_parent()}/dataPoints"
    )

    params: dict[str, Any] = {
        "pageSize": 1000,
        "filter": _heart_rate_filter(start_time, end_time),
    }

    if page_token:
        params["pageToken"] = page_token

    response = requests.get(
        url,
        headers=_auth_headers(),
        params=params,
        timeout=20,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            "Google Health heart-rate request failed. "
            f"HTTP {response.status_code}: {response.text}"
        ) from exc

    return response.json()


def _extract_sample_time(point: dict[str, Any]) -> datetime | None:
    """
    Extract a timestamp from a Google Health data point.

    This is intentionally defensive because API response shapes can be nested.
    """
    heart_rate = point.get("heartRate") or point.get("heart_rate")

    if isinstance(heart_rate, dict):
        sample_time = heart_rate.get("sampleTime") or heart_rate.get("sample_time")

        if isinstance(sample_time, dict):
            parsed_time = parse_iso8601(
                sample_time.get("physicalTime")
                or sample_time.get("physical_time")
                or sample_time.get("time")
            )

            if parsed_time:
                return parsed_time

    for key in ("startTime", "endTime", "time", "timestamp"):
        parsed_time = parse_iso8601(point.get(key))

        if parsed_time:
            return parsed_time

    return None


def _extract_bpm(point: dict[str, Any]) -> float | None:
    """
    Extract BPM from a Google Health data point.
    """
    heart_rate = point.get("heartRate") or point.get("heart_rate")

    possible_keys = (
        "bpm",
        "beatsPerMinute",
        "beats_per_minute",
        "beatsPerMinuteValue",
        "value",
    )

    if isinstance(heart_rate, dict):
        for key in possible_keys:
            value = heart_rate.get(key)

            if isinstance(value, (int, float)):
                return float(value)

    for key in possible_keys:
        value = point.get(key)

        if isinstance(value, (int, float)):
            return float(value)

    return None


def _parse_data_points(response_json: dict[str, Any]) -> list[HeartRateSample]:
    raw_points = response_json.get("dataPoints", [])
    samples: list[HeartRateSample] = []

    for point in raw_points:
        if not isinstance(point, dict):
            continue

        sample_time = _extract_sample_time(point)
        bpm = _extract_bpm(point)

        if sample_time is None or bpm is None:
            continue

        samples.append(
            HeartRateSample(
                timestamp=to_utc(sample_time),
                bpm=bpm,
            )
        )

    samples.sort(key=lambda sample: sample.timestamp)
    return samples


def get_heart_rate_samples(
    start_time: datetime,
    end_time: datetime,
) -> list[HeartRateSample]:
    """
    Fetch heart-rate samples from Google Health between start_time and end_time.

    This function does not care whether the range is 10 minutes, 30 minutes,
    or 60 minutes. The worker decides what range to request.
    """
    start_time = ensure_timezone_aware(start_time)
    end_time = ensure_timezone_aware(end_time)

    if end_time <= start_time:
        raise ValueError("end_time must be after start_time.")

    all_samples: list[HeartRateSample] = []
    page_token: str | None = None

    while True:
        response_json = _request_heart_rate_page(
            start_time=start_time,
            end_time=end_time,
            page_token=page_token,
        )

        all_samples.extend(_parse_data_points(response_json))

        page_token = response_json.get("nextPageToken")

        if not page_token:
            break

    all_samples.sort(key=lambda sample: sample.timestamp)
    return all_samples


def get_recent_heart_rate_samples(
    minutes_back: int,
    end_time: datetime | None = None,
) -> list[HeartRateSample]:
    """
    Convenience helper for quick checks.

    The full worker will usually call get_heart_rate_samples() with an exact
    start/end time based on the SmartWake windows.
    """
    if minutes_back <= 0:
        raise ValueError("minutes_back must be positive.")

    if end_time is None:
        end_time = datetime.now(timezone.utc)

    end_time = ensure_timezone_aware(end_time)
    start_time = end_time - timedelta(minutes=minutes_back)

    return get_heart_rate_samples(
        start_time=start_time,
        end_time=end_time,
    )