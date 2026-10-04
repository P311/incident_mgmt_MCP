"""BM25 keyword search, pinned against the same small fixture `test_query.py` uses."""

from __future__ import annotations

import pytest

from incident_management.search import build_index, search_incidents, tokenize


class TestTokenize:
    def test_lowercases_and_splits_on_non_word_characters(self):
        assert tokenize("Server outage, priority-High!") == ["server", "outage", "priority", "high"]

    def test_empty_string_has_no_tokens(self):
        assert tokenize("   ") == []


class TestBuildIndex:
    def test_one_document_per_case(self, events):
        index, fields_by_case = build_index(events)
        assert sorted(index.case_ids) == ["C1", "C2", "C3", "C4", "C5", "C6"]
        assert set(fields_by_case) == set(index.case_ids)

    def test_case_is_indexed_under_its_final_field_value(self, events):
        # C2 is escalated Medium -> High; its document must reflect High.
        _, fields_by_case = build_index(events)
        assert fields_by_case["C2"]["priority"] == "High"

    def test_missing_final_value_falls_back_to_the_last_non_missing_one(self, events):
        # C5's issue_type is missing on its final event; groupby("last") — the same
        # aggregation query.py's _case_frame uses — carries forward its earlier value.
        _, fields_by_case = build_index(events)
        assert fields_by_case["C5"]["issue_type"] == "Bug"


class TestSearchIncidents:
    def test_matching_cases_outrank_non_matching_cases(self, events):
        index, fields_by_case = build_index(events)
        result = search_incidents("Bug", index, fields_by_case, top_k=25)
        hit_ids = {hit.case_id for hit in result.hits}
        # C1, C2, C5, C6 end on issue_type "Bug"; C3, C4 end on "Incident".
        assert hit_ids == {"C1", "C2", "C5", "C6"}

    def test_scores_descend(self, events):
        index, fields_by_case = build_index(events)
        result = search_incidents("Bug Medium Website", index, fields_by_case, top_k=25)
        scores = [hit.score for hit in result.hits]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_limits_hit_count(self, events):
        index, fields_by_case = build_index(events)
        result = search_incidents("Bug", index, fields_by_case, top_k=1)
        assert len(result.hits) == 1

    def test_query_terms_reflect_the_tokenized_sentence(self, events):
        index, fields_by_case = build_index(events)
        result = search_incidents("Bug!", index, fields_by_case, top_k=25)
        assert result.query_terms == ("bug",)

    def test_no_matching_terms_returns_no_hits(self, events):
        index, fields_by_case = build_index(events)
        result = search_incidents("nonexistent term", index, fields_by_case, top_k=25)
        assert result.hits == ()

    def test_case_count_is_total_cases_regardless_of_match(self, events):
        index, fields_by_case = build_index(events)
        result = search_incidents("Bug", index, fields_by_case, top_k=25)
        assert result.case_count == 6


@pytest.mark.slow
class TestAgainstRealDataset:
    def test_search_finds_the_common_short_description(self, real_events):
        index, fields_by_case = build_index(real_events)
        result = search_incidents("Server outage", index, fields_by_case, top_k=25)
        assert result.case_count == 31588
        assert len(result.hits) == 25
        assert result.hits[0].fields["short_description"] == "Server outage"
