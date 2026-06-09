from __future__ import annotations

#need time zones for time stamp formatting strictly ISO8601
import os
from datetime import datetime, timezone
from typing import Any

#need google authentication tools
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials

#imports for system level flow
from database import load_google_token, save_google_token
from smartwake_logic import HeartRateSample, ensure_timezone_aware

#got from google project documentation
DEFAULT_GOOGLE_HEALTH_BASE_URL = "https://health.googleapis.com/v4"
DEFAULT_HEART_RATE_PARENT = "users/me/dataTypes/heart-rate"

#used to get all env values from render
def _get_env(name: str, default: str) -> str:
    value = os.getenv(name)

    if value is None or value.strip() == "":
        return default

    return value.strip()    #remove whitespace

#load in google urls
def get_google_health_base_url() -> str:
    return _get_env("GOOGLE_HEALTH_BASE_URL", DEFAULT_GOOGLE_HEALTH_BASE_URL)


def get_heart_rate_parent() -> str:
    return _get_env("GOOGLE_HEALTH_HEART_RATE_PARENT", DEFAULT_HEART_RATE_PARENT)

#all timestamps will be iso8601 so this converts to python date time
##can use datetime and iso functions from import to make timestamp handling easier
def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    return ensure_timezone_aware(parsed)

#converts to universal time zone for google auth GET expectations
def to_utc(moment: datetime) -> datetime:
    return ensure_timezone_aware(moment).astimezone(timezone.utc)

#converts an iso8601 to utc, uses to utc function we just built
def to_iso8601_utc_z(moment: datetime) -> str:
    return to_utc(moment).isoformat(timespec="seconds").replace("+00:00", "Z")

#take in google token stuff, return simple error if we dont have anything
def _load_credentials_from_database() -> Credentials:
    token_info = load_google_token()

    if not token_info:
        raise RuntimeError(
            "Google OAuth is not connected yet "
            "go to /auth/google on the web API"
        )

    return Credentials.from_authorized_user_info(token_info)

#take in tokens for runtime/actuation
#checks for refresh and nromal token
def get_credentials() -> Credentials:
    credentials = _load_credentials_from_database()

    if credentials.valid:
        return credentials

    if not credentials.refresh_token:
        raise RuntimeError(
            "No Refresh token found"
            "redo /auth/google process"
        )

    credentials.refresh(GoogleAuthRequest())
    save_google_token(credentials.to_json())
    return credentials

#google health expects certain headers for reques
#found format from google project, show authroization for user
#then asks for response back in json
def _auth_headers() -> dict[str, str]:
    credentials = get_credentials()

    return {
        "Authorization": f"Bearer {credentials.token}",
        "Accept": "application/json",
    }

#defines window were getting heart rate data for
#request is for any availalbe data in a certain window
#need to be utc ISO8601
def _heart_rate_filter(start_time: datetime, end_time: datetime) -> str:
    start = to_iso8601_utc_z(start_time)
    end = to_iso8601_utc_z(end_time)

    return (    #need >= so we dont get error on start
        f'heart_rate.sample_time.physical_time >= "{start}" '
        f'AND heart_rate.sample_time.physical_time < "{end}"'
    )

#request to google health
#gets one"page" of data which is any amount of data in the time period
#first we build the expected urls
#then request up to 1000 samples in the time window
#if we have a page already use that token to request for that page
def _request_heart_rate_page(
    start_time: datetime,
    end_time: datetime,
    page_token: str | None = None,
) -> dict[str, Any]:
    url = f"{get_google_health_base_url()}/{get_heart_rate_parent()}/dataPoints"

    params: dict[str, Any] = {
        "pageSize": 1000,
        "filter": _heart_rate_filter(start_time, end_time),
    }

    if page_token:
        params["pageToken"] = page_token

    #actually do the GET with the headers and params we made
    #if we dont get anything fail after 20 seconds for debugging
    response = requests.get(
        url,
        headers=_auth_headers(),
        params=params,
        timeout=20,
    )
    #return error like we did in debugging app can find what type of error we have
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            "Google Health heart-rate request failed. "
            f"HTTP {response.status_code}: {response.text}"
        ) from exc

    return response.json()



#google health data points are formatted with a timestamp and bpm
#need to extract each so we extract the time
#we check caml naming conventions just incase, hard to oefine that specific error so we j do both casses
def _extract_sample_time(point: dict[str, Any]) -> datetime | None:
    heart_rate = point.get("heartRate") or point.get("heart_rate")
    #check all names and stuff then just take in time and parse
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
    #second check just in case, found multiple sources or possibilites for format of data point
    #again hard to know its this specific error so try all types
    for key in ("startTime", "endTime", "time", "timestamp"):
        parsed_time = parse_iso8601(point.get(key))

        if parsed_time:
            return parsed_time

    return None

#same process as above just for bpm
#check all names just in case
#need to parse values as we take them in
def _extract_bpm(point: dict[str, Any]) -> float | None:
    heart_rate = point.get("heartRate") or point.get("heart_rate")

    possible_keys = (
        "bpm",
        "beatsPerMinute",
        "beats_per_minute",
        "beatsPerMinuteValue",
        "value",
    )

    def parse_bpm_value(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)

        if isinstance(value, str):
            stripped = value.strip()

            if stripped == "":
                return None

            try:
                return float(stripped)
            except ValueError:
                return None

        return None

    if isinstance(heart_rate, dict):
        for key in possible_keys:
            bpm = parse_bpm_value(heart_rate.get(key))

            if bpm is not None:
                return bpm

    for key in possible_keys:
        bpm = parse_bpm_value(point.get(key))

        if bpm is not None:
            return bpm

    return None

#convert raw json date into usable format
def _parse_data_points(response_json: dict[str, Any]) -> list[HeartRateSample]:
    raw_points = response_json.get("dataPoints", [])
    samples: list[HeartRateSample] = []
    #nly do dictionary and take in our two values into a new list
    for point in raw_points:
        if not isinstance(point, dict):
            continue

        sample_time = _extract_sample_time(point)
        bpm = _extract_bpm(point)
        #skip balnk data poionts
        if sample_time is None or bpm is None:
            continue

        samples.append(
            HeartRateSample(
                timestamp=to_utc(sample_time),
                bpm=bpm,
            )
        )
    #sort form oldest to newest
    samples.sort(key=lambda sample: sample.timestamp)
    return samples

#function for main, uses our built list of samples
#take in window and list
def get_heart_rate_samples(
    start_time: datetime,
    end_time: datetime,
) -> list[HeartRateSample]:
    start_time = ensure_timezone_aware(start_time)
    end_time = ensure_timezone_aware(end_time)

    #if end_time <= start_time:
    #   raise ValueError("end_time must be after start_time.")

    #create stuff for storing values
    all_samples: list[HeartRateSample] = []
    page_token: str | None = None

    #get our heart rate data and loop so we cna keep checking
    while True:
        response_json = _request_heart_rate_page(
            start_time=start_time,
            end_time=end_time,
            page_token=page_token,
        )

        #parse data then check if theres another page to get a new token for
        #if not just sort oldest to newest then return
        all_samples.extend(_parse_data_points(response_json))
        page_token = response_json.get("nextPageToken")

        if not page_token:
            break

    all_samples.sort(key=lambda sample: sample.timestamp)
    return all_samples
