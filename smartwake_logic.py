from __future__ import annotations
#import stuff for math and averaging over time
#need timestamp stuff
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from math import ceil
from statistics import mean
from typing import Sequence

#function as a FSM so these are our states
class WakeDecision(str, Enum):
    WAKE_NOW = "WAKE_NOW"
    KEEP_MONITORING = "KEEP_MONITORING"
    NOT_ENOUGH_DATA = "NOT_ENOUGH_DATA"
    CYCLE_COMPLETE = "CYCLE_COMPLETE"

#one heart rate sample should look like this
@dataclass(frozen=True)
class HeartRateSample:
    timestamp: datetime
    bpm: float

#config for all smartawke parameters
#timestamps expected iso8601
#two windows, 60-30min from wake time and 30min to wake time
#window 1 for calibration, take in all available samples
#window 2 for checking, check last 10 min each run
@dataclass(frozen=True)
class SmartWakeConfig:

    wake_time: str


    calibration_window_minutes: int = 30
    wake_window_minutes: int = 30

    analysis_window_minutes: int = 10

    calibration_min_samples: int = 10
    #25% high and low samples are outliers
    outlier_fraction: float = 0.25


    min_samples_required: int = 5

    #make sure POST time is 2 min ahead of best wake time for web api
    post_lead_minutes: int = 2

#what we look for in calibration stage
@dataclass(frozen=True)
class CalibrationStats:
    sample_count: int
    low_outlier_group: tuple[float, ...]
    high_outlier_group: tuple[float, ...]
    low_average: float | None
    high_average: float | None
    dynamic_threshold: float | None
    threshold_source: str

#recent data from final decision
@dataclass(frozen=True)
class HeartRateFeatures:
    sample_count: int
    latest_bpm: float
    average_bpm: float
    all_samples_above_threshold: bool

#all aspects of smart wake decision
#used to degine system level parameters so we dont overlap with other calsses
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
    all_samples_above_threshold: bool | None

#make sure each timestamp is an ok format
def ensure_timezone_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.astimezone()

    return moment

#current local time returned
def now_local() -> datetime:
    return datetime.now().astimezone()

#parse ISO8601 inso datetime for python
def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None

#handle and store set wake time as our system is centered around this value
#check for errors as this starts the system
#we can restart if we have a later time than the current
def parse_required_wake_time(wake_time: str) -> datetime:

    parsed = parse_iso8601(wake_time)

    if parsed is None:
        raise ValueError(
            "wake_time must be full ISO 8601 timestamp, "
            f"got {wake_time!r}"
        )

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "wake_time must be have timezone offset stuff, "
            f"got {wake_time!r}"
        )

    return parsed

#define our two windows
def get_smartwake_windows(
    config: SmartWakeConfig,
    now: datetime,
) -> tuple[datetime, datetime, datetime]:
    #if config.calibration_window_minutes <= 0:
        #raise ValueError("calibration_window_minutes must be positive")

    #if config.wake_window_minutes <= 0:
        #raise ValueError("wake_window_minutes must be positive")

    #check for timezone
    ensure_timezone_aware(now)

    #define deadlines and use that to create windows, use wake time - 60 minutes and wake time - 30 minutes
    wake_deadline = parse_required_wake_time(config.wake_time)
    wake_window_start = wake_deadline - timedelta(minutes=config.wake_window_minutes)
    calibration_window_start = wake_window_start - timedelta(
        minutes=config.calibration_window_minutes
    )

    return calibration_window_start, wake_window_start, wake_deadline

#gets samples from our list within a specific time
#used for the ten minutes check
def _filter_samples_between(
    samples: Sequence[HeartRateSample],
    start_time: datetime,
    end_time: datetime,
    include_end: bool,
) -> list[HeartRateSample]:
    filtered: list[HeartRateSample] = []
    start_time = ensure_timezone_aware(start_time)
    end_time = ensure_timezone_aware(end_time)

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

#get new dynamic threshold from calibration samples
#filter bottom and top 25% of all samples in 30 min window
#threshold is midpoint
def _compute_calibration_stats(
    calibration_samples: Sequence[HeartRateSample],
    config: SmartWakeConfig,
) -> CalibrationStats:
    sample_count = len(calibration_samples)
    #check for samples before we start logic stuff
    if sample_count < config.calibration_min_samples:
        return CalibrationStats(
            sample_count=sample_count,
            low_outlier_group=(),
            high_outlier_group=(),
            low_average=None,
            high_average=None,
            dynamic_threshold=None,
            threshold_source="not_enough_calibration_samples",
        )

    #if not (0 < config.outlier_fraction <= 0.5):
    #    raise ValueError("outlier_fraction must be between 0 and 0.5")

    #sort all samples from lowest to highest
    #high is average of top 25%
    #low is average from bottom 25%
    sorted_bpms = sorted(float(sample.bpm) for sample in calibration_samples)

    group_size = ceil(sample_count * config.outlier_fraction)
    group_size = max(1, min(group_size, sample_count // 2))

    low_group = tuple(sorted_bpms[:group_size])
    high_group = tuple(sorted_bpms[-group_size:])

    low_average = mean(low_group)
    high_average = mean(high_group)
    dynamic_threshold = (low_average + high_average) / 2.0
    #returnall valvulated values so we can see error potential
    return CalibrationStats(
        sample_count=sample_count,
        low_outlier_group=low_group,
        high_outlier_group=high_group,
        low_average=low_average,
        high_average=high_average,
        dynamic_threshold=dynamic_threshold,
        threshold_source="dynamic_midpoint",
    )

#checks samples against thershold
#need enough sapmles (5 in the past 10 min)
#average bpm and each sample sohld be above threshold
def _compute_features(
    samples: Sequence[HeartRateSample],
    threshold: float,
) -> HeartRateFeatures:
    if not samples:
        raise ValueError("Cannot compute features with no samples.")

    bpms = [float(sample.bpm) for sample in samples]
    return HeartRateFeatures(
        sample_count=len(bpms),
        latest_bpm=bpms[-1],
        average_bpm=mean(bpms),
        all_samples_above_threshold=all(bpm >= threshold for bpm in bpms),
    )

#have a placeholder blacnk result for when were not urnning
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
        all_samples_above_threshold=None,
    )

#main function
def evaluate_smart_wake(
    samples: Sequence[HeartRateSample],
    config: SmartWakeConfig,
    now: datetime | None = None,
) -> SmartWakeResult:
    if now is None:
        now = now_local()
    else:
        now = ensure_timezone_aware(now)

    #if config.analysis_window_minutes <= 0:
    #    raise ValueError("analysis_window_minutes must be positive")

    #if config.min_samples_required <= 0:
    #    raise ValueError("min_samples_required must be positive")

    #if config.post_lead_minutes < 0:
    #    raise ValueError("post_lead_minutes cannot be negative")

    calibration_window_start, wake_window_start, wake_deadline = get_smartwake_windows(
        config=config,
        now=now,
    )
    #establish now time and then all the window deadlines
    calibration_window_end = wake_window_start
    #define the 10 min window
    analysis_window_start = now - timedelta(minutes=config.analysis_window_minutes)
    analysis_window_end = now

    #simple checks for current time and if were in window
    inside_calibration_window = calibration_window_start <= now < calibration_window_end
    inside_wake_window = wake_window_start <= now < wake_deadline
    deadline_reached = now >= wake_deadline

    #take in all data in calibration window then calculate the new threshold
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

    #if we hit set wake time just shut down and do nothing, fast api setup for this alreaduy
    if deadline_reached:
        return _empty_result(
            decision=WakeDecision.CYCLE_COMPLETE,
            reason=(
                "Wake deadline reached without a best wake time "
                "cycle complete and no alarm_time posted"
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

    #keep monitoring/on standby if were not in calivration window
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

    #if in calibration window still standby, just collecting samples
    if inside_calibration_window:
        return _empty_result(
            decision=WakeDecision.KEEP_MONITORING,
            reason="Inside calibration window; collecting samples for the dynamic threshold.",
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

    if inside_wake_window:
        # If calibration did not run
        if calibration is None or calibration.dynamic_threshold is None:
            return _empty_result(
                decision=WakeDecision.NOT_ENOUGH_DATA,
                reason=(
                    "Inside active SmartWake window, but calibration did not "
                    "give us a value"
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

        #need at laest 5 samples for wake event
        if len(recent_samples) < config.min_samples_required:
            return _empty_result(
                decision=WakeDecision.NOT_ENOUGH_DATA,
                reason="Inside active SmartWake window, but fewer than 5 recent samples are available.",
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

        #take last 5 samples from the past 10 min window
        decision_samples = recent_samples[-config.min_samples_required :]

        # are these 5 samples above the threshold
        features = _compute_features(
            samples=decision_samples,
            threshold=calibration.dynamic_threshold,
        )

        #that means were good to wake now
        threshold_ok = features.all_samples_above_threshold

        #post time should be two minutes ahead of best wake time
        if threshold_ok:
            post_lead = timedelta(minutes=config.post_lead_minutes)
            alarm_time = min(now + post_lead, wake_deadline)

            return SmartWakeResult(
                decision=WakeDecision.WAKE_NOW,
                reason=(
                    "Inside SmartWake window; latest 5 samples are above "
                    "the dynamic threshold."
                ),
                #return all possible values to make debugging easier
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
                all_samples_above_threshold=features.all_samples_above_threshold,
            )

        failed_conditions: list[str] = []
        #if the samples arent good for wake now we j keep monitoring
        if not threshold_ok:
            failed_conditions.append("not all latest 5 samples are above the dynamic threshold")

        return SmartWakeResult(
            decision=WakeDecision.KEEP_MONITORING,
            reason="Inside active SmartWake window, but " + ", ".join(failed_conditions) + ".",
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
            all_samples_above_threshold=features.all_samples_above_threshold,
        )
