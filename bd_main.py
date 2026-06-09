from __future__ import annotations

import os
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

#this is the main file so we need to import everything that were using


from circadiarender_stuff import build_alarm_update_payload, get_schedule, post_settings
from database import (
    init_db,
    load_completed_event_key,
    load_last_posted_event_key,
    save_completed_event_key,
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

#get env functions for the render environment variables
#keeping it separated by type allows me to easily call a different function for each type
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

#take in all parameters, if not defined use fallback values
POLL_SECONDS = _get_int_env("POLL_SECONDS", 60)
POST_WAKE_EVENTS = _get_bool_env("POST_WAKE_EVENTS", True)

CALIBRATION_WINDOW_MINUTES = _get_int_env("CALIBRATION_WINDOW_MINUTES", 30)
WAKE_WINDOW_MINUTES = _get_int_env("WAKE_WINDOW_MINUTES", 30)
ANALYSIS_WINDOW_MINUTES = _get_int_env("ANALYSIS_WINDOW_MINUTES", 10)
CALIBRATION_MIN_SAMPLES = _get_int_env("CALIBRATION_MIN_SAMPLES", 10)
OUTLIER_FRACTION = _get_float_env("OUTLIER_FRACTION", 0.25)
MIN_SAMPLES_REQUIRED = _get_int_env("MIN_SAMPLES_REQUIRED", 5)
POST_LEAD_MINUTES = _get_int_env("POST_LEAD_MINUTES", 2)

#we parse from JSON to python but this does python to JSON for sending w/ JSON expectations
#handle all types, is easiest way to run through and define errors
def _json_safe(value: Any) -> Any:
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

#get the wake_time and build te smartwake config around it
#used for smartwake file and logic
def build_smartwake_config(schedule: dict[str, Any]) -> SmartWakeConfig:

    wake_time = schedule.get("wake_time")

    if not wake_time:
        raise RuntimeError("Schedule is missing required field 'wake_time'")

    return SmartWakeConfig(
        wake_time=str(wake_time),
        calibration_window_minutes=CALIBRATION_WINDOW_MINUTES,
        wake_window_minutes=WAKE_WINDOW_MINUTES,
        analysis_window_minutes=ANALYSIS_WINDOW_MINUTES,
        calibration_min_samples=CALIBRATION_MIN_SAMPLES,
        outlier_fraction=OUTLIER_FRACTION,
        min_samples_required=MIN_SAMPLES_REQUIRED,
        post_lead_minutes=POST_LEAD_MINUTES,
    )

#deadline defined by ISO wake_time, dont change anything
def _event_key_from_deadline(wake_deadline: datetime) -> str:
    return wake_deadline.isoformat(timespec="seconds")

#gets wake cycle event key from smart wake result to run 'fsm logic'
def _event_key(result: SmartWakeResult) -> str:
    return _event_key_from_deadline(result.wake_deadline)

#chceks if we posted for this cycle
def _already_posted(result: SmartWakeResult) -> bool:
    return load_last_posted_event_key() == _event_key(result)

#need to mark a POST
#these are the same as the other files we took care of in database and api the other files
def _mark_posted(result: SmartWakeResult) -> None:
    save_last_posted_event_key(_event_key(result))

#checks if the wake cycle is completed
def _already_completed_event_key(event_key: str) -> bool:
    return load_completed_event_key() == event_key

#need to mark wake cycle as complete if we hit the wake_time without a trigger
def _mark_completed_event(result: SmartWakeResult) -> None:
    save_completed_event_key(_event_key(result))

#save wht worker just did for debugging
#check all values so we can see why were tripping
def _save_status(
    *,
    checked_at: datetime,
    schedule: dict[str, Any] | None,
    result: SmartWakeResult | None,
    sample_count: int,
    posted: bool,
    post_response: dict[str, Any] | None,
    wake_event_key: str | None = None,
    skipped: bool = False,
    skipped_reason: str | None = None,
    error: str | None = None,
) -> None:
    status = {
        "checked_at": checked_at.isoformat(timespec="seconds"),
        "wake_event_key": wake_event_key,
        "sample_count": sample_count,
        "posted": posted,
        "skipped": skipped,
        "skipped_reason": skipped_reason,
        "post_response": post_response,
        "error": error,
        "schedule": schedule,
        "result": _json_safe(result) if result is not None else None,
    }

    save_worker_status(status)

#prints a readable version of the satus we just saved
#lets us see literally everything and what is wrong
def _print_result(
    *,
    checked_at: datetime,
    schedule: dict[str, Any],
    result: SmartWakeResult,
    sample_count: int,
    posted: bool,
) -> None:
    print("\ncheck for smartWake worker stuff")
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
    print(f"All decision samples above threshold: {result.all_samples_above_threshold}")

    if result.alarm_time is not None:
        print(f"Computed alarm_time: {result.alarm_time}")

    print(f"Posted to circadianode: {posted}")


#post inside wake window but we dont need to if we already hit wake_time
#only post when we define an alarm_time inside the wake window
#only post once
def maybe_post_alarm(
    *,
    schedule: dict[str, Any],
    result: SmartWakeResult,
) -> dict[str, Any] | None:

    if not POST_WAKE_EVENTS:
        print("POST_WAKE_EVENTS is false; not posting alarm_time")
        return None

    if result.decision != WakeDecision.WAKE_NOW:
        return None

    if result.alarm_time is None:
        raise RuntimeError("WAKE_NOW missing alarm_time")

    if _already_posted(result):
        print("alarm_time alreadsy POSTed this cycle")
        return None

    payload = build_alarm_update_payload(
        schedule=schedule,
        alarm_time=result.alarm_time,
    )

    post_response = post_settings(payload)
    _mark_posted(result)

    print("Posted alarm_time to circadianode /settings")
    print(f"alarm_time: {payload.get('alarm_time')}")

    return post_response

#do one full worker check to make sure the background worker is running smooth
#like what we were building abvoe in those functions, we just take in all relevant data and run the flow accordingly
def run_once() -> SmartWakeResult | None:
    checked_at = now_local()

    schedule = get_schedule()
    config = build_smartwake_config(schedule)

    calibration_start, _, wake_deadline = get_smartwake_windows(
        config=config,
        now=checked_at,
    )
    wake_event_key = _event_key_from_deadline(wake_deadline)

    if _already_completed_event_key(wake_event_key):
        skipped_reason = (
            "This ISO wake_time has already been ran, waiting for the API to "
            "return a future wake_time"
        )
        print("\nsmartwake worker check")
        print(f"Checked at: {checked_at.isoformat(timespec='seconds')}")
        print(f"wake_time: {schedule.get('wake_time')}")
        print(skipped_reason)

        _save_status(
            checked_at=checked_at,
            schedule=schedule,
            result=None,
            sample_count=0,
            posted=False,
            post_response=None,
            wake_event_key=wake_event_key,
            skipped=True,
            skipped_reason=skipped_reason,
        )
        return None

    should_fetch = calibration_start <= checked_at < wake_deadline
    samples = []

    if should_fetch:
        samples = get_heart_rate_samples(
            start_time=calibration_start,
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

    if result.decision in {WakeDecision.WAKE_NOW, WakeDecision.CYCLE_COMPLETE}:
        _mark_completed_event(result)

    _save_status(
        checked_at=checked_at,
        schedule=schedule,
        result=result,
        sample_count=len(samples),
        posted=posted,
        post_response=post_response,
        wake_event_key=wake_event_key,
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
    init_db()
    #start data base then print all parameters to make sure were good
    print("smartwake background worker started")
    print(f"Polling interval: {POLL_SECONDS} seconds")
    print(f"Calibration window: {CALIBRATION_WINDOW_MINUTES} minutes")
    print(f"Wake window: {WAKE_WINDOW_MINUTES} minutes")
    print(f"Analysis window: {ANALYSIS_WINDOW_MINUTES} minutes")
    print(f"Calibration min samples: {CALIBRATION_MIN_SAMPLES}")
    print(f"Outlier fraction: {OUTLIER_FRACTION}")
    print(f"Min decision samples: {MIN_SAMPLES_REQUIRED}")
    print(f"Post lead minutes: {POST_LEAD_MINUTES}")
    print(f"POST wake events: {POST_WAKE_EVENTS}")

    while True:
        checked_at = now_local()

        #run through the worker and return any errors, should be able to runt hrough all the way
        #handles errors with start up in case setup on render is incorrect
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

#always run the main infinitely if its opened directly
#works for background worker
if __name__ == "__main__":
    main()
