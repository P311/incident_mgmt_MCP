from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from .dataset import load_event_log

GroupField = Literal["priority", "issue_type", "report_channel"]
CounterMetric = Literal["reassignment_count", "reopen_count"]


@dataclass(frozen=True)
class DurationQuery:
    """Validated input for the processing-duration aggregation tool."""

    group_by: tuple[GroupField, ...] = ("priority", "issue_type", "report_channel")
    priority: tuple[str, ...] = ()
    issue_type: tuple[str, ...] = ()
    report_channel: tuple[str, ...] = ()
    timestamp_from: datetime | None = None
    timestamp_to: datetime | None = None

    def __post_init__(self) -> None:
        allowed = {"priority", "issue_type", "report_channel"}
        if not self.group_by or any(field not in allowed for field in self.group_by):
            raise ValueError("group_by must contain priority, issue_type, or report_channel")
        if self.timestamp_from and self.timestamp_to and self.timestamp_from > self.timestamp_to:
            raise ValueError("timestamp_from must be before timestamp_to")


@dataclass(frozen=True)
class DurationRow:
    group: dict[str, str]
    count: int
    mean_duration_seconds: float | None
    median_duration_seconds: float | None


@dataclass(frozen=True)
class DurationResult:
    metric: Literal["case_processing_duration"]
    duration_unit: Literal["seconds"]
    timestamp_from: str | None
    timestamp_to: str | None
    rows: tuple[DurationRow, ...]


@dataclass(frozen=True)
class DistributionQuery:
    """Validated input for the counter-distribution aggregation tool."""

    metric: CounterMetric = "reassignment_count"
    priority: tuple[str, ...] = ()
    issue_type: tuple[str, ...] = ()
    report_channel: tuple[str, ...] = ()
    timestamp_from: datetime | None = None
    timestamp_to: datetime | None = None

    def __post_init__(self) -> None:
        if self.metric not in {"reassignment_count", "reopen_count"}:
            raise ValueError("metric must be reassignment_count or reopen_count")
        if self.timestamp_from and self.timestamp_to and self.timestamp_from > self.timestamp_to:
            raise ValueError("timestamp_from must be before timestamp_to")


@dataclass(frozen=True)
class DistributionRow:
    value: int
    count: int
    share: float


@dataclass(frozen=True)
class DistributionResult:
    metric: CounterMetric
    case_count: int
    timestamp_from: str | None
    timestamp_to: str | None
    rows: tuple[DistributionRow, ...]


def counter_distribution(
    query: DistributionQuery, frame: pd.DataFrame | None = None
) -> DistributionResult:
    """Return the deterministic per-case distribution of a running incident counter.

    `reassignment_count` and `reopen_count` are running counters derived from the
    activity sequence (see `dataset.py`) and carried on every event, so a case
    contributes one observation: its maximum, which is also its final value.
    """
    events = load_event_log() if frame is None else frame.copy()
    cases = _case_frame(events, value=(query.metric, "max"))
    cases = _filter_cases(cases, query)
    cases = _apply_window(cases, query)
    case_count = int(cases["value"].count())
    counts = cases["value"].value_counts(dropna=True).sort_index()
    rows = tuple(
        DistributionRow(
            value=int(value),
            count=int(count),
            share=round(int(count) / case_count, 6) if case_count else 0.0,
        )
        for value, count in counts.items()
    )
    return DistributionResult(
        metric=query.metric,
        case_count=case_count,
        timestamp_from=_iso(query.timestamp_from),
        timestamp_to=_iso(query.timestamp_to),
        rows=rows,
    )


def processing_duration(query: DurationQuery, frame: pd.DataFrame | None = None) -> DurationResult:
    """Return deterministic case-duration aggregates for the requested dimensions."""
    events = load_event_log() if frame is None else frame.copy()
    cases = _case_frame(events)
    cases["duration_seconds"] = (cases["ended_at"] - cases["started_at"]).dt.total_seconds()
    cases = _filter_cases(cases, query)
    cases = _apply_window(cases, query)
    grouped = cases.groupby(list(query.group_by), dropna=False, sort=True)["duration_seconds"]
    rows = tuple(
        DurationRow(
            group={field: _output_value(field, key) for field, key in zip(query.group_by, key_tuple)},
            count=int(values.count()),
            mean_duration_seconds=_rounded(values.mean()),
            median_duration_seconds=_rounded(values.median()),
        )
        for key_tuple, values in _keyed_groups(grouped)
    )
    return DurationResult(
        metric="case_processing_duration",
        duration_unit="seconds",
        timestamp_from=_iso(query.timestamp_from),
        timestamp_to=_iso(query.timestamp_to),
        rows=rows,
    )


def _case_frame(events: pd.DataFrame, **extra) -> pd.DataFrame:
    """Collapse events to one row per case.

    A case is attributed by its final event, so a case whose priority, issue_type or
    report_channel changes mid-life is reported under the value it ended with.
    """
    aggregations = {
        "priority": ("priority", "last"),
        "issue_type": ("issue_type", "last"),
        "report_channel": ("report_channel", "last"),
        "started_at": ("timestamp", "min"),
        "ended_at": ("timestamp", "max"),
        **extra,
    }
    return events.groupby("case_id", sort=False).agg(**aggregations)


def _filter_cases(cases: pd.DataFrame, query: DurationQuery | DistributionQuery) -> pd.DataFrame:
    """Filter on case-level attributes, so filtering and grouping share one definition."""
    filters = {
        "priority": query.priority,
        "issue_type": query.issue_type,
        "report_channel": query.report_channel,
    }
    for field, values in filters.items():
        if values:
            cases = cases[cases[field].isin(values)]
    return cases


def _apply_window(cases: pd.DataFrame, query: DurationQuery | DistributionQuery) -> pd.DataFrame:
    """Select cases by their first event; a window never truncates a case."""
    if query.timestamp_from is not None:
        cases = cases[cases["started_at"] >= query.timestamp_from]
    if query.timestamp_to is not None:
        cases = cases[cases["started_at"] <= query.timestamp_to]
    return cases


def _keyed_groups(grouped):
    for key, values in grouped:
        yield (key if isinstance(key, tuple) else (key,)), values


def _output_value(field: str, value: object) -> str:
    return "" if pd.isna(value) else str(value)


def _rounded(value: float) -> float | None:
    return None if pd.isna(value) else round(float(value), 3)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None