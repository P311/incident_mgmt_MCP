"""Keyword search over the incident event log, ranked with Okapi BM25.

The document indexed for each case concatenates its short description with its
other descriptive fields — issue type, priority, report channel, and who reported
and resolved it — taken from the case's final event, matching the attribution rule
every other tool in this package uses.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import pandas as pd

TOKEN_RE = re.compile(r"[a-z0-9]+")

# Fields concatenated into each case's searchable document. Pure bookkeeping columns
# (variant, timestamps, derived counters) are excluded.
SEARCH_FIELDS = (
    "short_description",
    "issue_type",
    "priority",
    "report_channel",
    "reporter",
    "resolver",
)


def tokenize(text: str) -> list[str]:
    """Lowercase word split shared by indexing and querying, so both sides agree."""
    return TOKEN_RE.findall(text.lower())


@dataclass(frozen=True)
class SearchHit:
    case_id: str
    score: float
    fields: dict[str, str]


@dataclass(frozen=True)
class SearchResult:
    query_terms: tuple[str, ...]
    case_count: int
    hits: tuple[SearchHit, ...]


class BM25Index:
    """Okapi BM25 over one document per case, built from an inverted index."""

    K1 = 1.5
    B = 0.75

    def __init__(self, case_ids: list[str], token_lists: list[list[str]]) -> None:
        self.case_ids = case_ids
        self._lengths = [len(tokens) for tokens in token_lists]
        self._avg_length = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        self._postings: dict[str, list[tuple[int, int]]] = {}
        for idx, tokens in enumerate(token_lists):
            for term, freq in Counter(tokens).items():
                self._postings.setdefault(term, []).append((idx, freq))
        n_docs = len(case_ids)
        self._idf = {
            term: math.log((n_docs - len(postings) + 0.5) / (len(postings) + 0.5) + 1)
            for term, postings in self._postings.items()
        }

    def score(self, terms: list[str]) -> dict[int, float]:
        """Sum BM25 term scores per document index, over documents matching >=1 term."""
        scores: dict[int, float] = {}
        for term in terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = self._idf[term]
            for idx, freq in postings:
                norm = 1 - self.B + self.B * (self._lengths[idx] / self._avg_length)
                scores[idx] = scores.get(idx, 0.0) + idf * (freq * (self.K1 + 1)) / (freq + self.K1 * norm)
        return scores


def build_index(frame: pd.DataFrame) -> tuple[BM25Index, dict[str, dict[str, str]]]:
    """Collapse events to one searchable document per case, and index it.

    A case is attributed by its final event, so a search hit reflects how the case
    ended up rather than how it started — the same rule `query.py` uses.
    """
    present = [field for field in SEARCH_FIELDS if field in frame.columns]
    cases = frame.groupby("case_id", sort=False)[present].last()

    case_ids: list[str] = []
    fields_by_case: dict[str, dict[str, str]] = {}
    token_lists: list[list[str]] = []
    for case_id, row in cases.iterrows():
        case_id = str(case_id)
        values = {field: str(value) for field, value in row.items() if pd.notna(value)}
        case_ids.append(case_id)
        fields_by_case[case_id] = values
        token_lists.append(tokenize(" ".join(values.values())))

    return BM25Index(case_ids, token_lists), fields_by_case


def search_incidents(
    sentence: str,
    index: BM25Index,
    fields_by_case: dict[str, dict[str, str]],
    top_k: int = 25,
) -> SearchResult:
    """Split `sentence` into words and rank cases by BM25 score, highest first."""
    terms = tokenize(sentence)
    scores = index.score(terms)
    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[:top_k]
    hits = tuple(
        SearchHit(case_id=index.case_ids[idx], score=round(score, 4), fields=fields_by_case[index.case_ids[idx]])
        for idx, score in ranked
    )
    return SearchResult(query_terms=tuple(terms), case_count=len(index.case_ids), hits=hits)
