"""Loader behaviour: schema contract, sentinel handling, dtypes, ordering, and the
counters derived from the activity sequence."""

from __future__ import annotations

import pandas as pd
import pytest

from incident_management.dataset import DATASET_PATH, load_event_log

HEADER = (
    "Case ID;Variant;Priority;Reporter;Timestamp;Event;Issue Type;Resolver;"
    "Report Channel;Short Description;Customer Satisfaction"
)


def _row(case, event, timestamp, *, priority="Medium", resolver="Sam", satisfaction=3):
    return (
        f"{case};Variant 1;{priority};Alice;{timestamp};{event};Bug;{resolver};Website;"
        f"Application crash;{satisfaction}"
    )


@pytest.fixture
def csv_path(tmp_path):
    """A small file in the real dataset's exact format."""
    rows = [
        _row("INC01", "Ticket created", "1/3/2024 09:00", resolver=""),
        _row("INC01", "Ticket assigned to level 1 support", "1/3/2024 10:00"),
        _row("INC01", "Level 1 escalates to level 2 support", "1/3/2024 11:00"),
        _row("INC01", "Ticket closed", "3/3/2024 09:00"),
        _row("INC02", "Ticket created", "2/3/2024 09:00", resolver=""),
        _row("INC02", "Ticket closed", "2/3/2024 12:00"),
        _row("INC02", "Ticket reopened by customer", "2/3/2024 13:00", resolver=""),
        _row("INC02", "Ticket closed", "2/3/2024 14:00"),
        # Malformed row: a blank activity, which the real file also carries once.
        _row("INC03", " ", "4/3/2024 09:00"),
        # Two events sharing one timestamp: order must come from file order, not be shuffled.
        _row("INC03", "Ticket created", "4/3/2024 10:00"),
        _row("INC03", "WIP - level 1 support", "4/3/2024 10:00"),
    ]
    path = tmp_path / "events.csv"
    path.write_text("\n".join([HEADER, *rows]) + "\n")
    return path


class TestSchemaContract:
    def test_process_mining_aliases_are_present(self, csv_path):
        frame = load_event_log(csv_path)
        assert {"case_id", "event", "timestamp"} <= set(frame.columns)
        assert frame["case_id"].tolist() == ["INC01"] * 4 + ["INC02"] * 4 + ["INC03"] * 2

    def test_missing_file_is_reported(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Dataset not found"):
            load_event_log(tmp_path / "absent.csv")

    def test_wrong_schema_is_rejected(self, tmp_path):
        path = tmp_path / "other.csv"
        path.write_text("case_id;variant;event\nA;x;New\n")
        with pytest.raises(ValueError, match="missing required columns"):
            load_event_log(path)


class TestParsing:
    def test_dates_use_day_first_format(self, csv_path):
        frame = load_event_log(csv_path)
        # "1/3/2024" is 1 March, not 3 January.
        assert frame["timestamp"].min() == pd.Timestamp("2024-03-01 09:00")

    def test_blank_activity_row_is_dropped(self, csv_path):
        """INC03's blank-event row cannot be placed in a process flow, so it is dropped."""
        frame = load_event_log(csv_path)
        assert len(frame[frame["case_id"] == "INC03"]) == 2

    def test_empty_resolver_becomes_missing(self, csv_path):
        frame = load_event_log(csv_path)
        assert frame["resolver"].isna().sum() == 3  # two "Ticket created", one reopen

    def test_customer_satisfaction_is_a_nullable_integer(self, csv_path):
        frame = load_event_log(csv_path)
        assert str(frame["customer_satisfaction"].dtype) == "Int64"


class TestDerivedCounters:
    def test_reassignment_count_is_a_running_counter(self, csv_path):
        """INC01 escalates once; the counter steps 0 -> 0 -> 1 -> 1 across its events."""
        frame = load_event_log(csv_path)
        inc01 = frame[frame["case_id"] == "INC01"]
        assert inc01["reassignment_count"].tolist() == [0, 0, 1, 1]

    def test_reopen_count_is_a_running_counter(self, csv_path):
        """INC02 is reopened once, after its first close."""
        frame = load_event_log(csv_path)
        inc02 = frame[frame["case_id"] == "INC02"]
        assert inc02["reopen_count"].tolist() == [0, 0, 1, 1]

    def test_first_assignment_is_not_counted_as_a_reassignment(self, csv_path):
        """"Ticket assigned to level 1 support" is the initial handoff, not a *re*-assignment."""
        frame = load_event_log(csv_path)
        first_assignment = frame[
            (frame["case_id"] == "INC01") & (frame["event"] == "Ticket assigned to level 1 support")
        ]
        assert first_assignment["reassignment_count"].iloc[0] == 0

    def test_counters_never_decrease_within_a_case(self, csv_path):
        frame = load_event_log(csv_path)
        for column in ("reassignment_count", "reopen_count"):
            grouped = frame.groupby("case_id", sort=False)[column]
            assert (grouped.last() == grouped.max()).all(), column


class TestOrdering:
    def test_events_are_sorted_by_case_then_time(self, csv_path):
        frame = load_event_log(csv_path)
        for _, group in frame.groupby("case_id"):
            assert group["timestamp"].is_monotonic_increasing

    def test_tied_timestamps_keep_file_order(self, csv_path):
        """INC03's two surviving events share a timestamp; sort is stable, so file order wins."""
        frame = load_event_log(csv_path)
        tied = frame[frame["case_id"] == "INC03"]
        assert tied["event"].tolist() == ["Ticket created", "WIP - level 1 support"]


@pytest.mark.slow
class TestRealDataset:
    def test_shape(self, real_events):
        assert len(real_events) == 242900
        assert real_events["case_id"].nunique() == 31588

    def test_event_timestamps_are_complete(self, real_events):
        assert real_events["timestamp"].isna().sum() == 0

    def test_counters_never_decrease_within_a_case(self, real_events):
        """The rule the distribution tool depends on: max per case == final value."""
        for column in ("reassignment_count", "reopen_count"):
            grouped = real_events.groupby("case_id", sort=False)[column]
            assert (grouped.last() == grouped.max()).all(), column

    def test_dataset_path_points_at_the_real_file(self):
        assert DATASET_PATH.name == "incident_event_log.csv.zip"
