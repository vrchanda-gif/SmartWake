from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from math import ceil
from statistics import mean
from typing import Sequence


class WakeDecision(str, Enum):
    WAKE_NOW = "WAKE_NOW"
    KEEP_MONITORING = "KEEP_MONITORING"
    NOT_ENOUGH_DATA = "NOT_ENOUGH_DATA"


@dataclass(frozen=True)
class HeartRateSample:
    timestamp: datetime
    bpm: float


@dataclass(frozen=True)
class SmartWakeConfig:
    wake_time: str

    # Timeline:
    # wake_time - 60 min to wake_time - 30 min = calibration window
    # wake_time - 30 min to wake_time          = active SmartWake window
    calibration_window_minutes: int = 30
    wake_window_minutes: int = 30

    # In the active SmartWake window, we still analyze only the most recent
    # analysis_window_minutes of samples.
    analysis_window_minutes: int = 10

    # Calibration settings
    calibration_min_samples: int = 10
    outlier_fraction: float = 0.25
    fallback_bpm_threshold: float = 75.0
    use_fallback_threshold: bool = True

    # Decision settings
    min_samples_required: int = 5
    max_bpm_range: float = 8.0
    require_all_samples_below_threshold: bool = True
    require_non_increasing_trend: bool = False

    # Deadline / posting behavior
    deadline_grace_minutes: int = 5
    post_lead_minutes: int = 2


@dataclass(frozen=True)
class CalibrationStats:
    sample_count: int
    low_outlier_group: tuple[float, ...]
    high_outlier_group: tuple[float, ...]
    low_average: float | None
    high_average: float | None
    dynamic_threshold: float | None
    threshold_source: str | None


@dataclass(frozen=True)
class HeartRateFeatures:
    sample_count: int
    latest_bpm: float
    average_bpm: float
    min_bpm: float
    max_bpm: float
    bpm_range: float
    trend_delta_bpm: float
    all_samples_below_threshold: bool
    non_increasing_trend: bool


@dataclass(frozen=True)
class SmartWakeResult:
    decision: WakeDecision
    reason: str

    alarm_time: datetime | None

    now: datetime
    calibration_window_start: datetime
    calibration_window_end: datetime
    wake_window_start: datetime
    wake_deadline: datetime
    analysis_window_start: datetime
    analysis_window_end: datetime

    inside_calibration_window: bool
    inside_wake_window: bool
    deadline_reached: bool

    calibration: CalibrationStats | None

    recent_sample_count: int
    decision_sample_count: int

    latest_bpm: float | None
    average_bpm: float | None
    min_bpm: float | None
    max_bpm: float | None
    bpm_range: float | None
    trend_delta_bpm: float | None
    all_samples_below_threshold: bool | None
    non_increasing_trend: bool | None


def ensure_timezone_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.astimezone()

    return moment


def now_local() -> datetime:
    return datetime.now().astimezone()


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    return ensure_timezone_aware(parsed)


def _parse_hhmm(wake_time: str) -> time:
    try:
        hour_str, minute_str = wake_time.split(":")
        hour = int(hour_str)
        minute = int(minute_str)
    except ValueError as exc:
        raise ValueError(
            f"wake_time must be ISO8601 or HH:MM format, got {wake_time!r}"
        ) from exc

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(
            f"wake_time must be a valid 24-hour time, got {wake_time!r}"
        )

    return time(hour=hour, minute=minute)


def _wake_deadline_for_current_cycle(
    wake_time: str,
    now: datetime,
    deadline_grace_minutes: int,
) -> datetime:
    if deadline_grace_minutes < 0:
        raise ValueError("deadline_grace_minutes cannot be negative")

    parsed_iso = parse_iso8601(wake_time)

    if parsed_iso is not None:
        return parsed_iso

    parsed_time = _parse_hhmm(wake_time)

    deadline = now.replace(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        second=0,
        microsecond=0,
    )

    grace = timedelta(minutes=deadline_grace_minutes)

    if now > deadline + grace:
        deadline += timedelta(days=1)

    return deadline


def get_smartwake_windows(
    config: SmartWakeConfig,
    now: datetime,
) -> tuple[datetime, datetime, datetime]:
    if config.calibration_window_minutes <= 0:
        raise ValueError("calibration_window_minutes must be positive")

    if config.wake_window_minutes <= 0:
        raise ValueError("wake_window_minutes must be positive")

    now = ensure_timezone_aware(now)

    wake_deadline = _wake_deadline_for_current_cycle(
        wake_time=config.wake_time,
        now=now,
        deadline_grace_minutes=config.deadline_grace_minutes,
    )

    wake_window_start = wake_deadline - timedelta(
        minutes=config.wake_window_minutes
    )

    calibration_window_start = wake_window_start - timedelta(
        minutes=config.calibration_window_minutes
    )

    return calibration_window_start, wake_window_start, wake_deadline


def _filter_samples_between(
    samples: Sequence[HeartRateSample],
    start_time: datetime,
    end_time: datetime,
    include_end: bool,
) -> list[HeartRateSample]:
    filtered: list[HeartRateSample] = []

    for sample in samples:
        sample_time = ensure_timezone_aware(sample.timestamp)

        if include_end:
            inside = start_time <= sample_time <= end_time
        else:
            inside = start_time <= sample_time < end_time

        if inside:
            filtered.append(
                HeartRateSample(
                    timestamp=sample_time,
                    bpm=float(sample.bpm),
                )
            )

    filtered.sort(key=lambda sample: sample.timestamp)
    return filtered


def _compute_calibration_stats(
    calibration_samples: Sequence[HeartRateSample],
    config: SmartWakeConfig,
) -> CalibrationStats:
    sample_count = len(calibration_samples)

    if sample_count < config.calibration_min_samples:
        if config.use_fallback_threshold:
            return CalibrationStats(
                sample_count=sample_count,
                low_outlier_group=(),
                high_outlier_group=(),
                low_average=None,
                high_average=None,
                dynamic_threshold=config.fallback_bpm_threshold,
                threshold_source="fallback_not_enough_calibration_samples",
            )

        return CalibrationStats(
            sample_count=sample_count,
            low_outlier_group=(),
            high_outlier_group=(),
            low_average=None,
            high_average=None,
            dynamic_threshold=None,
            threshold_source="not_enough_calibration_samples",
        )

    if not (0 < config.outlier_fraction <= 0.5):
        raise ValueError("outlier_fraction must be between 0 and 0.5")

    sorted_bpms = sorted(float(sample.bpm) for sample in calibration_samples)

    group_size = ceil(sample_count * config.outlier_fraction)
    group_size = max(1, min(group_size, sample_count // 2))

    low_group = tuple(sorted_bpms[:group_size])
    high_group = tuple(sorted_bpms[-group_size:])

    low_average = mean(low_group)
    high_average = mean(high_group)

    dynamic_threshold = (low_average + high_average) / 2.0

    return CalibrationStats(
        sample_count=sample_count,
        low_outlier_group=low_group,
        high_outlier_group=high_group,
        low_average=low_average,
        high_average=high_average,
        dynamic_threshold=dynamic_threshold,
        threshold_source="dynamic_midpoint",
    )


def _compute_features(
    samples: Sequence[HeartRateSample],
    threshold: float,
) -> HeartRateFeatures:
    if not samples:
        raise ValueError("Cannot compute features with no samples.")

    bpms = [float(sample.bpm) for sample in samples]

    first_bpm = bpms[0]
    latest_bpm = bpms[-1]
    min_bpm = min(bpms)
    max_bpm = max(bpms)

    return HeartRateFeatures(
        sample_count=len(bpms),
        latest_bpm=latest_bpm,
        average_bpm=mean(bpms),
        min_bpm=min_bpm,
        max_bpm=max_bpm,
        bpm_range=max_bpm - min_bpm,
        trend_delta_bpm=latest_bpm - first_bpm,
        all_samples_below_threshold=all(bpm <= threshold for bpm in bpms),
        non_increasing_trend=latest_bpm <= first_bpm,
    )


def _empty_result(
    *,
    decision: WakeDecision,
    reason: str,
    alarm_time: datetime | None,
    now: datetime,
    calibration_window_start: datetime,
    calibration_window_end: datetime,
    wake_window_start: datetime,
    wake_deadline: datetime,
    analysis_window_start: datetime,
    analysis_window_end: datetime,
    inside_calibration_window: bool,
    inside_wake_window: bool,
    deadline_reached: bool,
    calibration: CalibrationStats | None,
    recent_sample_count: int,
    decision_sample_count: int = 0,
) -> SmartWakeResult:
    return SmartWakeResult(
        decision=decision,
        reason=reason,
        alarm_time=alarm_time,
        now=now,
        calibration_window_start=calibration_window_start,
        calibration_window_end=calibration_window_end,
        wake_window_start=wake_window_start,
        wake_deadline=wake_deadline,
        analysis_window_start=analysis_window_start,
        analysis_window_end=analysis_window_end,
        inside_calibration_window=inside_calibration_window,
        inside_wake_window=inside_wake_window,
        deadline_reached=deadline_reached,
        calibration=calibration,
        recent_sample_count=recent_sample_count,
        decision_sample_count=decision_sample_count,
        latest_bpm=None,
        average_bpm=None,
        min_bpm=None,
        max_bpm=None,
        bpm_range=None,
        trend_delta_bpm=None,
        all_samples_below_threshold=None,
        non_increasing_trend=None,
    )


def evaluate_smart_wake(
    samples: Sequence[HeartRateSample],
    config: SmartWakeConfig,
    now: datetime | None = None,
) -> SmartWakeResult:
    if now is None:
        now = now_local()
    else:
        now = ensure_timezone_aware(now)

    if config.analysis_window_minutes <= 0:
        raise ValueError("analysis_window_minutes must be positive")

    if config.min_samples_required <= 0:
        raise ValueError("min_samples_required must be positive")

    if config.post_lead_minutes < 0:
        raise ValueError("post_lead_minutes cannot be negative")

    calibration_window_start, wake_window_start, wake_deadline = (
        get_smartwake_windows(config=config, now=now)
    )

    calibration_window_end = wake_window_start

    analysis_window_start = now - timedelta(
        minutes=config.analysis_window_minutes
    )

    analysis_window_end = now

    deadline_grace = timedelta(minutes=config.deadline_grace_minutes)

    inside_calibration_window = (
        calibration_window_start <= now < calibration_window_end
    )

    inside_wake_window = wake_window_start <= now < wake_deadline

    deadline_reached = wake_deadline <= now <= wake_deadline + deadline_grace

    calibration_samples = _filter_samples_between(
        samples=samples,
        start_time=calibration_window_start,
        end_time=calibration_window_end,
        include_end=False,
    )

    calibration = None

    if now >= calibration_window_start:
        calibration = _compute_calibration_stats(
            calibration_samples=calibration_samples,
            config=config,
        )

    recent_samples = _filter_samples_between(
        samples=samples,
        start_time=analysis_window_start,
        end_time=analysis_window_end,
        include_end=True,
    )

    if deadline_reached:
        return _empty_result(
            decision=WakeDecision.WAKE_NOW,
            reason="Final wake deadline reached; forcing alarm_time to wake_time.",
            alarm_time=wake_deadline,
            now=now,
            calibration_window_start=calibration_window_start,
            calibration_window_end=calibration_window_end,
            wake_window_start=wake_window_start,
            wake_deadline=wake_deadline,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            inside_calibration_window=inside_calibration_window,
            inside_wake_window=inside_wake_window,
            deadline_reached=deadline_reached,
            calibration=calibration,
            recent_sample_count=len(recent_samples),
        )

    if now < calibration_window_start:
        return _empty_result(
            decision=WakeDecision.KEEP_MONITORING,
            reason="Current time is before the calibration window.",
            alarm_time=None,
            now=now,
            calibration_window_start=calibration_window_start,
            calibration_window_end=calibration_window_end,
            wake_window_start=wake_window_start,
            wake_deadline=wake_deadline,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            inside_calibration_window=inside_calibration_window,
            inside_wake_window=inside_wake_window,
            deadline_reached=deadline_reached,
            calibration=calibration,
            recent_sample_count=len(recent_samples),
        )

    if inside_calibration_window:
        return _empty_result(
            decision=WakeDecision.KEEP_MONITORING,
            reason=(
                "Inside calibration window. Collecting heart-rate samples to "
                "build the dynamic threshold."
            ),
            alarm_time=None,
            now=now,
            calibration_window_start=calibration_window_start,
            calibration_window_end=calibration_window_end,
            wake_window_start=wake_window_start,
            wake_deadline=wake_deadline,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            inside_calibration_window=inside_calibration_window,
            inside_wake_window=inside_wake_window,
            deadline_reached=deadline_reached,
            calibration=calibration,
            recent_sample_count=len(recent_samples),
        )

    if not inside_wake_window:
        return _empty_result(
            decision=WakeDecision.KEEP_MONITORING,
            reason="Current time is outside the active Smart Wake window.",
            alarm_time=None,
            now=now,
            calibration_window_start=calibration_window_start,
            calibration_window_end=calibration_window_end,
            wake_window_start=wake_window_start,
            wake_deadline=wake_deadline,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            inside_calibration_window=inside_calibration_window,
            inside_wake_window=inside_wake_window,
            deadline_reached=deadline_reached,
            calibration=calibration,
            recent_sample_count=len(recent_samples),
        )

    if calibration is None or calibration.dynamic_threshold is None:
        return _empty_result(
            decision=WakeDecision.NOT_ENOUGH_DATA,
            reason=(
                "Inside active Smart Wake window, but not enough calibration "
                "data is available to build a dynamic threshold."
            ),
            alarm_time=None,
            now=now,
            calibration_window_start=calibration_window_start,
            calibration_window_end=calibration_window_end,
            wake_window_start=wake_window_start,
            wake_deadline=wake_deadline,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            inside_calibration_window=inside_calibration_window,
            inside_wake_window=inside_wake_window,
            deadline_reached=deadline_reached,
            calibration=calibration,
            recent_sample_count=len(recent_samples),
        )

    if len(recent_samples) < config.min_samples_required:
        return _empty_result(
            decision=WakeDecision.NOT_ENOUGH_DATA,
            reason=(
                "Inside active Smart Wake window, but not enough recent "
                "heart-rate samples are available for a reliable decision."
            ),
            alarm_time=None,
            now=now,
            calibration_window_start=calibration_window_start,
            calibration_window_end=calibration_window_end,
            wake_window_start=wake_window_start,
            wake_deadline=wake_deadline,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            inside_calibration_window=inside_calibration_window,
            inside_wake_window=inside_wake_window,
            deadline_reached=deadline_reached,
            calibration=calibration,
            recent_sample_count=len(recent_samples),
        )

    decision_samples = recent_samples[-config.min_samples_required:]

    features = _compute_features(
        samples=decision_samples,
        threshold=calibration.dynamic_threshold,
    )

    if config.require_all_samples_below_threshold:
        threshold_ok = features.all_samples_below_threshold
    else:
        threshold_ok = features.average_bpm <= calibration.dynamic_threshold

    bpm_stable = features.bpm_range <= config.max_bpm_range

    if config.require_non_increasing_trend:
        trend_ok = features.non_increasing_trend
    else:
        trend_ok = True

    if threshold_ok and bpm_stable and trend_ok:
        post_lead = timedelta(minutes=config.post_lead_minutes)
        alarm_time = min(now + post_lead, wake_deadline)

        return SmartWakeResult(
            decision=WakeDecision.WAKE_NOW,
            reason=(
                "Inside active Smart Wake window; recent heart-rate samples "
                "are below the dynamic threshold and stable."
            ),
            alarm_time=alarm_time,
            now=now,
            calibration_window_start=calibration_window_start,
            calibration_window_end=calibration_window_end,
            wake_window_start=wake_window_start,
            wake_deadline=wake_deadline,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            inside_calibration_window=inside_calibration_window,
            inside_wake_window=inside_wake_window,
            deadline_reached=deadline_reached,
            calibration=calibration,
            recent_sample_count=len(recent_samples),
            decision_sample_count=len(decision_samples),
            latest_bpm=features.latest_bpm,
            average_bpm=features.average_bpm,
            min_bpm=features.min_bpm,
            max_bpm=features.max_bpm,
            bpm_range=features.bpm_range,
            trend_delta_bpm=features.trend_delta_bpm,
            all_samples_below_threshold=features.all_samples_below_threshold,
            non_increasing_trend=features.non_increasing_trend,
        )

    failed_conditions: list[str] = []

    if not threshold_ok:
        if config.require_all_samples_below_threshold:
            failed_conditions.append(
                "not all recent samples are below the dynamic threshold"
            )
        else:
            failed_conditions.append(
                "average BPM is above the dynamic threshold"
            )

    if not bpm_stable:
        failed_conditions.append("BPM range is too large")

    if not trend_ok:
        failed_conditions.append("heart rate is increasing")

    return SmartWakeResult(
        decision=WakeDecision.KEEP_MONITORING,
        reason="Inside active Smart Wake window, but " + ", ".join(failed_conditions) + ".",
        alarm_time=None,
        now=now,
        calibration_window_start=calibration_window_start,
        calibration_window_end=calibration_window_end,
        wake_window_start=wake_window_start,
        wake_deadline=wake_deadline,
        analysis_window_start=analysis_window_start,
        analysis_window_end=analysis_window_end,
        inside_calibration_window=inside_calibration_window,
        inside_wake_window=inside_wake_window,
        deadline_reached=deadline_reached,
        calibration=calibration,
        recent_sample_count=len(recent_samples),
        decision_sample_count=len(decision_samples),
        latest_bpm=features.latest_bpm,
        average_bpm=features.average_bpm,
        min_bpm=features.min_bpm,
        max_bpm=features.max_bpm,
        bpm_range=features.bpm_range,
        trend_delta_bpm=features.trend_delta_bpm,
        all_samples_below_threshold=features.all_samples_below_threshold,
        non_increasing_trend=features.non_increasing_trend,
    )