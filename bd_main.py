from __future__ import annotations

import os
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from circadian_client import (
    build_alarm_update_payload,
    get_schedule,
    post_settings,
    to_iso8601_seconds,
)
from database import (
    load_last_posted_event_key,
    save_last_posted_event_key,
    save_worker_status,
)
from google_health_auth import get_heart_rate_samples
from smartwake_logic import (
    SmartWakeConfig,
    SmartWakeResult,
    WakeDecision,
    evaluate_smart_wake,
    get_smartwake_windows,
    now_local,
)


def _get_env(name: str, default: str) -> str:
    value = os.getenv(name)

    if value is None or value.strip() == "":
        return default

    return value.strip()


def _get_int_env(name: str, default: int) -> int:
    raw_value = _get_env(name, str(default))

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}") from exc


def _get_float_env(name: str, default: float) -> float:
    raw_value = _get_env(name, str(default))

    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw_value!r}") from exc


def _get_bool_env(name: str, default: bool) -> bool:
    raw_value = _get_env(name, str(default)).lower()

    if raw_value in ("true", "1", "yes", "y", "on"):
        return True

    if raw_value in ("false", "0", "no", "n", "off"):
        return False

    raise ValueError(f"{name} must be true or false, got {raw_value!r}")


POLL_SECONDS = _get_int_env("POLL_SECONDS", 60)
POST_WAKE_EVENTS = _get_bool_env("POST_WAKE_EVENTS", True)

CALIBRATION_WINDOW_MINUTES = _get_int_env("CALIBRATION_WINDOW_MINUTES", 30)
WAKE_WINDOW_MINUTES = _get_int_env("WAKE_WINDOW_MINUTES", 30)
ANALYSIS_WINDOW_MINUTES = _get_int_env("ANALYSIS_WINDOW_MINUTES", 10)

CALIBRATION_MIN_SAMPLES = _get_int_env("CALIBRATION_MIN_SAMPLES", 10)
OUTLIER_FRACTION = _get_float_env("OUTLIER_FRACTION", 0.25)
FALLBACK_BPM_THRESHOLD = _get_float_env("FALLBACK_BPM_THRESHOLD", 75.0)
USE_FALLBACK_THRESHOLD = _get_bool_env("USE_FALLBACK_THRESHOLD", True)

MIN_SAMPLES_REQUIRED = _get_int_env("MIN_SAMPLES_REQUIRED", 5)
MAX_BPM_RANGE = _get_float_env("MAX_BPM_RANGE", 8.0)
REQUIRE_ALL_SAMPLES_BELOW_THRESHOLD = _get_bool_env(
    "REQUIRE_ALL_SAMPLES_BELOW_THRESHOLD",
    True,
)
REQUIRE_NON_INCREASING_TREND = _get_bool_env(
    "REQUIRE_NON_INCREASING_TREND",
    False,
)

DEADLINE_GRACE_MINUTES = _get_int_env("DEADLINE_GRACE_MINUTES", 5)
POST_LEAD_MINUTES = _get_int_env("POST_LEAD_MINUTES", 2)


def _json_safe(value: Any) -> Any:
    """
    Convert dataclasses, datetimes, enums, tuples, and nested values into
    JSON-safe values that can be stored in Postgres as worker status.
    """
    if is_dataclass(value):
        return _json_safe(asdict(value))

    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]

    return value


def build_smartwake_config(schedule: dict[str, Any]) -> SmartWakeConfig:
    """
    Build the SmartWakeConfig from the circadianode schedule.

    The only required schedule field for the logic is wake_time.
    The other values are preserved later when we POST the full payload back
    to circadianode /settings.
    """
    wake_time = schedule.get("wake_time")

    if not wake_time:
        raise RuntimeError("Schedule is missing required field 'wake_time'.")

    return SmartWakeConfig(
        wake_time=str(wake_time),
        calibration_window_minutes=CALIBRATION_WINDOW_MINUTES,
        wake_window_minutes=WAKE_WINDOW_MINUTES,
        analysis_window_minutes=ANALYSIS_WINDOW_MINUTES,
        calibration_min_samples=CALIBRATION_MIN_SAMPLES,
        outlier_fraction=OUTLIER_FRACTION,
        fallback_bpm_threshold=FALLBACK_BPM_THRESHOLD,
        use_fallback_threshold=USE_FALLBACK_THRESHOLD,
        min_samples_required=MIN_SAMPLES_REQUIRED,
        max_bpm_range=MAX_BPM_RANGE,
        require_all_samples_below_threshold=REQUIRE_ALL_SAMPLES_BELOW_THRESHOLD,
        require_non_increasing_trend=REQUIRE_NON_INCREASING_TREND,
        deadline_grace_minutes=DEADLINE_GRACE_MINUTES,
        post_lead_minutes=POST_LEAD_MINUTES,
    )


def _event_key(result: SmartWakeResult) -> str:
    """
    Create a stable key for this wake cycle.

    We use the wake deadline down to the minute. This prevents duplicate posts
    for the same alarm cycle, but still allows a new post if the user changes
    the wake_time.
    """
    return result.wake_deadline.isoformat(timespec="minutes")


def _already_posted(result: SmartWakeResult) -> bool:
    previous_key = load_last_posted_event_key()
    return previous_key == _event_key(result)


def _mark_posted(result: SmartWakeResult) -> None:
    save_last_posted_event_key(_event_key(result))


def _should_fetch_samples(
    config: SmartWakeConfig,
    checked_at: datetime,
) -> tuple[bool, datetime, datetime, datetime]:
    """
    Decide whether it is worth fetching heart-rate samples.

    We fetch samples only once we are at or after the calibration window start.

    We fetch from:
        calibration_window_start → checked_at

    That gives smartwake_logic.py enough data to:
        - build the dynamic threshold from the first 30 minutes
        - analyze recent samples from the active wake window
    """
    calibration_start, wake_window_start, wake_deadline = get_smartwake_windows(
        config=config,
        now=checked_at,
    )

    deadline_grace_end = wake_deadline + timedelta(
        minutes=config.deadline_grace_minutes
    )

    should_fetch = calibration_start <= checked_at <= deadline_grace_end

    return should_fetch, calibration_start, wake_window_start, wake_deadline


def _save_status(
    *,
    checked_at: datetime,
    schedule: dict[str, Any] | None,
    result: SmartWakeResult | None,
    sample_count: int,
    posted: bool,
    post_response: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    """
    Save the worker's latest status to Postgres.

    This is useful because Render background workers do not have a web page.
    Later we can expose this through /worker/status if desired.
    """
    status = {
        "checked_at": checked_at.isoformat(timespec="seconds"),
        "sample_count": sample_count,
        "posted": posted,
        "post_response": post_response,
        "error": error,
        "schedule": schedule,
        "result": _json_safe(result) if result is not None else None,
    }

    save_worker_status(status)


def _print_result(
    *,
    checked_at: datetime,
    schedule: dict[str, Any],
    result: SmartWakeResult,
    sample_count: int,
    posted: bool,
) -> None:
    print("\nSmartWake worker check")
    print(f"Checked at: {checked_at.isoformat(timespec='seconds')}")
    print(f"wake_time: {schedule.get('wake_time')}")
    print(f"sleep_time: {schedule.get('sleep_time')}")
    print(f"temperature: {schedule.get('temperature')}")
    print(f"humidity: {schedule.get('humidity')}")
    print(f"current alarm_time: {schedule.get('alarm_time')}")
    print(f"Heart-rate samples fetched: {sample_count}")
    print(f"Decision: {result.decision.value}")
    print(f"Reason: {result.reason}")
    print(f"Calibration window start: {result.calibration_window_start}")
    print(f"Calibration window end: {result.calibration_window_end}")
    print(f"Wake window start: {result.wake_window_start}")
    print(f"Wake deadline: {result.wake_deadline}")

    if result.calibration is not None:
        print(f"Calibration samples: {result.calibration.sample_count}")
        print(f"Low avg: {result.calibration.low_average}")
        print(f"High avg: {result.calibration.high_average}")
        print(f"Dynamic threshold: {result.calibration.dynamic_threshold}")
        print(f"Threshold source: {result.calibration.threshold_source}")

    print(f"Recent sample count: {result.recent_sample_count}")
    print(f"Decision sample count: {result.decision_sample_count}")
    print(f"Latest BPM: {result.latest_bpm}")
    print(f"Average BPM: {result.average_bpm}")
    print(f"BPM range: {result.bpm_range}")

    if result.alarm_time is not None:
        print(f"Computed alarm_time: {result.alarm_time}")

    print(f"Posted to circadianode: {posted}")


def maybe_post_alarm(
    *,
    schedule: dict[str, Any],
    result: SmartWakeResult,
) -> dict[str, Any] | None:
    """
    If the logic says WAKE_NOW, POST the full updated payload to circadianode.

    Important:
    circadianode /settings expects the full payload, so we preserve the current
    schedule and only update alarm_time.
    """
    if not POST_WAKE_EVENTS:
        print("POST_WAKE_EVENTS is false; not posting alarm_time.")
        return None

    if result.decision != WakeDecision.WAKE_NOW:
        return None

    if result.alarm_time is None:
        raise RuntimeError("WAKE_NOW result is missing alarm_time.")

    if _already_posted(result):
        print("Alarm already posted for this wake cycle; skipping duplicate POST.")
        return None

    payload = build_alarm_update_payload(
        schedule=schedule,
        alarm_time=result.alarm_time,
    )

    post_response = post_settings(payload)

    _mark_posted(result)

    print("Posted alarm_time to circadianode /settings.")
    print(f"alarm_time: {payload.get('alarm_time')}")

    return post_response


def run_once() -> SmartWakeResult:
    """
    Run one full SmartWake check.

    This is intentionally one clean cycle so it is easy to test later.
    main() simply calls this repeatedly.
    """
    checked_at = now_local()

    schedule = get_schedule()
    config = build_smartwake_config(schedule)

    should_fetch, sample_start, _, _ = _should_fetch_samples(
        config=config,
        checked_at=checked_at,
    )

    samples = []

    if should_fetch:
        samples = get_heart_rate_samples(
            start_time=sample_start,
            end_time=checked_at,
        )

    result = evaluate_smart_wake(
        samples=samples,
        config=config,
        now=checked_at,
    )

    post_response = maybe_post_alarm(
        schedule=schedule,
        result=result,
    )

    posted = post_response is not None

    _save_status(
        checked_at=checked_at,
        schedule=schedule,
        result=result,
        sample_count=len(samples),
        posted=posted,
        post_response=post_response,
    )

    _print_result(
        checked_at=checked_at,
        schedule=schedule,
        result=result,
        sample_count=len(samples),
        posted=posted,
    )

    return result


def main() -> None:
    print("SmartWake worker started.")
    print(f"Polling interval: {POLL_SECONDS} seconds")
    print(f"Calibration window: {CALIBRATION_WINDOW_MINUTES} minutes")
    print(f"Wake window: {WAKE_WINDOW_MINUTES} minutes")
    print(f"Analysis window: {ANALYSIS_WINDOW_MINUTES} minutes")
    print(f"Calibration min samples: {CALIBRATION_MIN_SAMPLES}")
    print(f"Outlier fraction: {OUTLIER_FRACTION}")
    print(f"Fallback BPM threshold: {FALLBACK_BPM_THRESHOLD}")
    print(f"Min decision samples: {MIN_SAMPLES_REQUIRED}")
    print(f"Max BPM range: {MAX_BPM_RANGE}")
    print(f"Post lead minutes: {POST_LEAD_MINUTES}")
    print(f"POST wake events: {POST_WAKE_EVENTS}")

    while True:
        checked_at = now_local()

        try:
            run_once()

        except Exception as error:
            error_message = str(error)

            print("\nSmartWake worker error:")
            print(error_message)
            print("Worker will retry next cycle.\n")

            try:
                _save_status(
                    checked_at=checked_at,
                    schedule=None,
                    result=None,
                    sample_count=0,
                    posted=False,
                    post_response=None,
                    error=error_message,
                )
            except Exception as status_error:
                print("Failed to save worker error status:")
                print(status_error)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()