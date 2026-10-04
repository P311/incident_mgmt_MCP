"""Aggregation semantics, pinned against a fixture small enough to verify by hand."""

from __future__ import annotations

from datetime import datetime

import pytest

from incident_management.query import (
    DistributionQuery,
    DurationQuery,
    counter_distribution,
    processing_duration,
)

DAY = 86400.0


def _by_group(result, field="priority"):
    return {row.group[field]: row for row in result.rows}


class TestFinalEventAttribution:
    """A case is reported under the value it ended with, not the one it started with."""

    def test_escalated_case_counts_as_its_final_priority(self, events):
        rows = _by_group(processing_duration(DurationQuery(group_by=("priority",)), events))
        # C2 escalated Medium -> High, so it lands in High, not Medium.
        assert rows["High"].count == 1
        assert "Medium" in rows and rows["Medium"].count == 4  # C1, C4, C5, C6

    def test_downgraded_case_counts_as_its_final_priority(self, events):
        rows = _by_group(processing_duration(DurationQuery(group_by=("priority",)), events))
        # C3 went High -> Low, so Low holds exactly C3.
        assert rows["Low"].count == 1

    def test_every_case_lands_in_exactly_one_group(self, events):
        result = processing_duration(DurationQuery(group_by=("priority",)), events)
        assert sum(row.count for row in result.rows) == events["case_id"].nunique()

    def test_missing_final_value_does_not_drop_the_case(self, events):
        result = processing_duration(DurationQuery(group_by=("issue_type",)), events)
        assert sum(row.count for row in result.rows) == events["case_id"].nunique()


class TestFilterMatchesGrouping:
    """The regression this suite exists for: filtering and grouping must agree."""

    @pytest.mark.parametrize("value", ["High", "Medium", "Low"])
    def test_filtered_count_equals_grouped_count(self, events, value):
        grouped = _by_group(processing_duration(DurationQuery(group_by=("priority",)), events))
        filtered = processing_duration(
            DurationQuery(group_by=("priority",), priority=(value,)), events
        )
        assert len(filtered.rows) == 1
        assert filtered.rows[0].count == grouped[value].count

    def test_filter_agrees_across_both_tools(self, events):
        duration = processing_duration(
            DurationQuery(group_by=("priority",), priority=("High",)), events
        )
        distribution = counter_distribution(
            DistributionQuery(metric="reassignment_count", priority=("High",)), events
        )
        assert duration.rows[0].count == distribution.case_count

    def test_filtering_does_not_truncate_duration(self, events):
        """A filter selects cases; it must not clip a case to its matching events."""
        unfiltered = _by_group(processing_duration(DurationQuery(group_by=("priority",)), events))
        filtered = processing_duration(
            DurationQuery(group_by=("priority",), priority=("High",)), events
        )
        # C2 ran 03-01 -> 03-05 but only became High on 03-02. Full span must survive.
        assert filtered.rows[0].mean_duration_seconds == 4 * DAY
        assert filtered.rows[0].mean_duration_seconds == unfiltered["High"].mean_duration_seconds


class TestDuration:
    def test_duration_spans_first_to_last_event(self, events):
        rows = _by_group(processing_duration(DurationQuery(group_by=("priority",)), events))
        assert rows["Low"].mean_duration_seconds == 1 * DAY  # C3: 03-01 -> 03-02

    def test_mean_and_median_over_several_cases(self, events):
        result = processing_duration(
            DurationQuery(group_by=("priority",), priority=("Medium",)), events
        )
        # C1=2d, C4=3d, C5=1d, C6=1d -> mean 1.75d, median 1.5d
        assert result.rows[0].mean_duration_seconds == pytest.approx(1.75 * DAY)
        assert result.rows[0].median_duration_seconds == pytest.approx(1.5 * DAY)

    def test_multi_dimension_grouping(self, events):
        result = processing_duration(DurationQuery(group_by=("priority", "report_channel")), events)
        assert all({"priority", "report_channel"} == set(row.group) for row in result.rows)
        assert sum(row.count for row in result.rows) == 6


class TestTimeWindow:
    def test_window_selects_by_case_start(self, events):
        result = processing_duration(
            DurationQuery(
                group_by=("priority",),
                timestamp_from=datetime(2024, 3, 1),
                timestamp_to=datetime(2024, 3, 31),
            ),
            events,
        )
        assert sum(row.count for row in result.rows) == 5  # C6 starts in April

    def test_window_does_not_truncate_a_straddling_case(self, events):
        """C2 starts 03-01 and ends 03-05; a window ending 03-03 keeps its full duration."""
        result = processing_duration(
            DurationQuery(
                group_by=("priority",),
                priority=("High",),
                timestamp_to=datetime(2024, 3, 3),
            ),
            events,
        )
        assert result.rows[0].mean_duration_seconds == 4 * DAY

    def test_reversed_window_is_rejected(self):
        with pytest.raises(ValueError, match="timestamp_from must be before"):
            DurationQuery(timestamp_from=datetime(2024, 5, 1), timestamp_to=datetime(2024, 3, 1))


class TestCounterDistribution:
    def test_case_contributes_its_maximum_counter(self, events):
        result = counter_distribution(DistributionQuery(metric="reassignment_count"), events)
        by_value = {row.value: row.count for row in result.rows}
        # C4's counter runs 0 -> 1 -> 2; it must appear once, at 2 (not summed to 3).
        assert by_value == {0: 5, 2: 1}

    def test_reopen_counter(self, events):
        result = counter_distribution(DistributionQuery(metric="reopen_count"), events)
        assert {row.value: row.count for row in result.rows} == {0: 5, 1: 1}

    def test_counts_and_shares_are_consistent(self, events):
        result = counter_distribution(DistributionQuery(metric="reassignment_count"), events)
        assert sum(row.count for row in result.rows) == result.case_count == 6
        assert sum(row.share for row in result.rows) == pytest.approx(1.0, abs=1e-5)

    def test_rows_ascend_by_value(self, events):
        result = counter_distribution(DistributionQuery(metric="reassignment_count"), events)
        values = [row.value for row in result.rows]
        assert values == sorted(values)

    def test_empty_selection_does_not_divide_by_zero(self, events):
        result = counter_distribution(
            DistributionQuery(metric="reopen_count", issue_type=("Nonexistent Type",)), events
        )
        assert result.case_count == 0 and result.rows == ()

    def test_invalid_metric_is_rejected(self):
        with pytest.raises(ValueError, match="metric must be"):
            DistributionQuery(metric="customer_satisfaction")  # type: ignore[arg-type]

    def test_invalid_group_by_is_rejected(self):
        with pytest.raises(ValueError, match="group_by must contain"):
            DurationQuery(group_by=("resolver",))  # type: ignore[arg-type]


@pytest.mark.slow
class TestAgainstRealDataset:
    """A few totals that would catch a schema or attribution regression."""

    def test_all_cases_are_grouped(self, real_events):
        for dimension in ("priority", "report_channel", "issue_type"):
            result = processing_duration(DurationQuery(group_by=(dimension,)), real_events)
            assert sum(row.count for row in result.rows) == 31588, dimension

    def test_counter_totals(self, real_events):
        for metric in ("reassignment_count", "reopen_count"):
            result = counter_distribution(DistributionQuery(metric=metric), real_events)
            assert result.case_count == 31588
            assert sum(row.count for row in result.rows) == 31588

    def test_filter_and_group_agree_on_real_data(self, real_events):
        grouped = _by_group(processing_duration(DurationQuery(group_by=("priority",)), real_events))
        filtered = processing_duration(
            DurationQuery(group_by=("priority",), priority=("High",)), real_events
        )
        assert filtered.rows[0].count == grouped["High"].count == 6466
