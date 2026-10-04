"""Shared fixtures.

Most tests run against a hand-built event log small enough that every expected
number can be worked out by hand. Tests that need the real ~243k-row CSV ask for
`real_events` and are marked `slow`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from incident_management.dataset import DATASET_PATH, load_event_log


def _event(
    case,
    event,
    day,
    *,
    priority,
    report_channel,
    issue_type,
    resolver=None,
    reassign=0,
    reopen=0,
):
    return {
        "case_id": case,
        "event": event,
        "timestamp": pd.Timestamp(f"2024-03-{day:02d} 12:00"),
        "priority": priority,
        "issue_type": issue_type,
        "report_channel": report_channel,
        "resolver": resolver,
        "reassignment_count": reassign,
        "reopen_count": reopen,
    }


@pytest.fixture
def events() -> pd.DataFrame:
    """Six cases covering the behaviours the query layer has to get right.

    - C1 stays Medium priority throughout, never reassigned.
    - C2 is *escalated* mid-life (Medium -> High): the case that separates
      first-event from final-event attribution.
    - C3 is *downgraded* mid-life (High -> Low), the mirror of C2.
    - C4 is reassigned twice and reopened once.
    - C5 has a missing issue_type on its final event.
    - C6 starts in a later month, so time-window tests have something to exclude.
    """
    rows = [
        # C1: 2024-03-01 -> 03-03, duration 2 days, no reassignment
        _event("C1", "Ticket created", 1, priority="Medium", report_channel="Website", issue_type="Bug"),
        _event(
            "C1", "Ticket closed", 3, priority="Medium", report_channel="Website", issue_type="Bug",
            reassign=0,
        ),
        # C2: escalated. First event Medium, final event High.
        _event(
            "C2", "Ticket created", 1, priority="Medium", report_channel="Website", issue_type="Bug"
        ),
        _event(
            "C2", "WIP - level 1 support", 2, priority="High", report_channel="Website", issue_type="Bug"
        ),
        _event(
            "C2", "Ticket closed", 5, priority="High", report_channel="Website", issue_type="Bug"
        ),
        # C3: downgraded. First event High, final event Low.
        _event(
            "C3", "Ticket created", 1, priority="High", report_channel="Email", issue_type="Incident"
        ),
        _event(
            "C3", "Ticket closed", 2, priority="Low", report_channel="Email", issue_type="Incident"
        ),
        # C4: reassigned twice, reopened once
        _event(
            "C4", "Ticket created", 1, priority="Medium", report_channel="Email", issue_type="Incident"
        ),
        _event(
            "C4", "Level 1 escalates to level 2 support", 2, priority="Medium", report_channel="Email",
            issue_type="Incident", reassign=1,
        ),
        _event(
            "C4", "Ticket closed", 4, priority="Medium", report_channel="Email", issue_type="Incident",
            reassign=2, reopen=1,
        ),
        # C5: issue_type missing on the final event
        _event(
            "C5", "Ticket created", 1, priority="Medium", report_channel="Website", issue_type="Bug"
        ),
        _event(
            "C5", "Ticket closed", 2, priority="Medium", report_channel="Website", issue_type=None
        ),
        # C6: starts in April, outside a March window
        _event(
            "C6", "Ticket created", 1, priority="Medium", report_channel="Website", issue_type="Bug"
        ),
        _event(
            "C6", "Ticket closed", 2, priority="Medium", report_channel="Website", issue_type="Bug"
        ),
    ]
    frame = pd.DataFrame(rows)
    april = frame["case_id"] == "C6"
    frame.loc[april, "timestamp"] = frame.loc[april, "timestamp"] + pd.Timedelta(days=31)
    return frame.sort_values(["case_id", "timestamp"], kind="stable").reset_index(drop=True)


@pytest.fixture(scope="session")
def real_events() -> pd.DataFrame:
    """The actual dataset, loaded once per session."""
    if not DATASET_PATH.is_file():
        pytest.skip(f"dataset not present at {DATASET_PATH}")
    return load_event_log()
