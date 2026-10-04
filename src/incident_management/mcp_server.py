"""MCP server exposing the incident event log over three of MCP's surfaces.

Tools answer deterministic aggregation queries; the `incident_report` prompt hands
the caller's model a brief with the data attached; `write_incident_report` goes the
other way and samples the *client's* model, returning a finished report.

Runs over stdio: JSON-RPC frames own stdout, so every diagnostic must go to
stderr. Anything printed to stdout corrupts the protocol stream.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any

import pandas as pd
from mcp.server.mcpserver import MCPServer, Message, Resolve, Sample, UserMessage
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    CreateMessageResult,
    ModelPreferences,
    SamplingMessage,
    TextContent,
)

from .dataset import load_event_log
from .query import (
    DistributionQuery,
    DurationQuery,
    counter_distribution,
    processing_duration,
)
from .search import BM25Index, build_index, search_incidents

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("incident-management-mcp")

server = MCPServer(
    name="incident-management",
    version="0.1.0",
    instructions=(
        "Deterministic aggregations over an incident management event log "
        "(242,900 events across 31,588 cases). A case is attributed by its final "
        "event, so filters and grouping share one definition. Filter values are "
        "exact strings such as 'High' or 'Website' — call list_dimension_values "
        "first rather than guessing them. Use keyword_search to find incidents "
        "matching free-text terms via BM25. For a written report, use the "
        "incident_report prompt, or write_incident_report when the client "
        "supports sampling."
    ),
)


@lru_cache(maxsize=1)
def _events() -> pd.DataFrame:
    """Load the event log once, on first use, so the handshake stays fast."""
    frame = load_event_log()
    logger.info("loaded %d events across %d cases", len(frame), frame["case_id"].nunique())
    return frame


@lru_cache(maxsize=1)
def _search_index() -> tuple[BM25Index, dict[str, dict[str, str]]]:
    """Build the BM25 index once, on first search, over the same cached event log."""
    return build_index(_events())


DIMENSIONS = ("priority", "issue_type", "report_channel")


def _parse_timestamp(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(
            f"{field} must be ISO-8601, e.g. '2016-03-01' or '2016-03-01T00:00:00'; got {value!r}"
        ) from exc


@lru_cache(maxsize=1)
def _known_values() -> dict[str, frozenset[str]]:
    """Valid case-level values per dimension, used to reject silent-empty filters."""
    cases = _events().groupby("case_id")[list(DIMENSIONS)].last()
    return {dim: frozenset(cases[dim].dropna().astype(str)) for dim in DIMENSIONS}


def _checked_filter(dimension: str, values: list[str] | None) -> tuple[str, ...]:
    """Reject unknown filter values, which would otherwise return an empty result."""
    if not values:
        return ()
    known = _known_values()[dimension]
    unknown = [value for value in values if value not in known]
    if unknown:
        sample = sorted(known)[:6]
        raise ToolError(
            f"unknown {dimension} value(s) {unknown}; expected exact strings such as "
            f"{sample} — call list_dimension_values({dimension!r}) for the full list"
        )
    return tuple(values)


@server.tool(
    title="List dimension values",
    description=(
        "List the exact values available for a filter dimension, with the number of "
        "cases carrying each. Call this before filtering: e.g. priority is 'High', "
        "not '1 - High'."
    ),
)
def list_dimension_values(dimension: str) -> dict[str, Any]:
    """Return every value of `dimension` (priority, issue_type or report_channel) with its case count."""
    if dimension not in DIMENSIONS:
        raise ToolError(f"dimension must be one of {list(DIMENSIONS)}; got {dimension!r}")
    counts = _events().groupby("case_id")[dimension].last().value_counts().sort_index()
    return {
        "dimension": dimension,
        "values": [{"value": str(value), "case_count": int(count)} for value, count in counts.items()],
    }


@server.tool(
    title="Case processing duration",
    description=(
        "Aggregate case processing duration (first event to last event) grouped by "
        "priority, issue_type and/or report_channel. Returns count, mean and median "
        "seconds per group."
    ),
)
def case_processing_duration(
    group_by: list[str],
    priority: list[str] | None = None,
    issue_type: list[str] | None = None,
    report_channel: list[str] | None = None,
    timestamp_from: str | None = None,
    timestamp_to: str | None = None,
) -> dict[str, Any]:
    """Group cases by one or more dimensions and report their duration statistics."""
    unknown_dims = [field for field in group_by if field not in DIMENSIONS]
    if not group_by or unknown_dims:
        raise ToolError(f"group_by must be a non-empty subset of {list(DIMENSIONS)}; got {group_by}")
    query = DurationQuery(
        group_by=tuple(group_by),
        priority=_checked_filter("priority", priority),
        issue_type=_checked_filter("issue_type", issue_type),
        report_channel=_checked_filter("report_channel", report_channel),
        timestamp_from=_parse_timestamp(timestamp_from, "timestamp_from"),
        timestamp_to=_parse_timestamp(timestamp_to, "timestamp_to"),
    )
    result = processing_duration(query, _events())
    return {
        "metric": result.metric,
        "duration_unit": result.duration_unit,
        "timestamp_from": result.timestamp_from,
        "timestamp_to": result.timestamp_to,
        "rows": [
            {
                "group": row.group,
                "count": row.count,
                "mean_duration_seconds": row.mean_duration_seconds,
                "median_duration_seconds": row.median_duration_seconds,
            }
            for row in result.rows
        ],
    }


@server.tool(
    title="Counter distribution",
    description=(
        "Distribution of reassignment_count or reopen_count across cases. Each case "
        "contributes its final counter value; rows give value, case count and share. "
        "Both counters are derived from the activity sequence, not raw columns."
    ),
)
def counter_value_distribution(
    metric: str,
    priority: list[str] | None = None,
    issue_type: list[str] | None = None,
    report_channel: list[str] | None = None,
    timestamp_from: str | None = None,
    timestamp_to: str | None = None,
) -> dict[str, Any]:
    """Report how many cases were reassigned or reopened N times, for each N."""
    if metric not in ("reassignment_count", "reopen_count"):
        raise ToolError(f"metric must be 'reassignment_count' or 'reopen_count'; got {metric!r}")
    query = DistributionQuery(
        metric=metric,  # type: ignore[arg-type]
        priority=_checked_filter("priority", priority),
        issue_type=_checked_filter("issue_type", issue_type),
        report_channel=_checked_filter("report_channel", report_channel),
        timestamp_from=_parse_timestamp(timestamp_from, "timestamp_from"),
        timestamp_to=_parse_timestamp(timestamp_to, "timestamp_to"),
    )
    result = counter_distribution(query, _events())
    return {
        "metric": result.metric,
        "case_count": result.case_count,
        "timestamp_from": result.timestamp_from,
        "timestamp_to": result.timestamp_to,
        "rows": [
            {"value": row.value, "count": row.count, "share": row.share} for row in result.rows
        ],
    }


MAX_SEARCH_RESULTS = 100


@server.tool(
    title="Keyword search incidents",
    description=(
        "BM25 keyword search over incidents. Splits `sentence` into words and ranks "
        "cases by relevance across their descriptive fields (short_description, "
        "issue_type, priority, report_channel, reporter, resolver). Returns the top "
        "25 matches by default, each with its score and the field values it matched on."
    ),
)
def keyword_search(sentence: str, top_k: int = 25) -> dict[str, Any]:
    """Tokenize `sentence` and return the top-scoring cases under BM25."""
    if not sentence or not sentence.strip():
        raise ToolError("sentence must not be empty")
    if not 1 <= top_k <= MAX_SEARCH_RESULTS:
        raise ToolError(f"top_k must be between 1 and {MAX_SEARCH_RESULTS}; got {top_k}")
    index, fields_by_case = _search_index()
    result = search_incidents(sentence, index, fields_by_case, top_k)
    if not result.query_terms:
        raise ToolError(f"sentence {sentence!r} contains no searchable words")
    return {
        "query_terms": list(result.query_terms),
        "case_count": result.case_count,
        "results": [
            {"case_id": hit.case_id, "score": hit.score, "fields": hit.fields} for hit in result.hits
        ],
    }


REPORT_AUDIENCES: dict[str, str] = {
    "operations": (
        "an operations lead who owns the queue: lead with the groups that are slowest or "
        "most churn-prone, name dimension values exactly as they appear in the data, and "
        "keep the supporting numbers in the prose"
    ),
    "executive": (
        "a non-technical executive: lead with the single sentence that matters, carry at "
        "most five numbers in the body, and speak in whole days or hours rather than seconds"
    ),
}

REPORT_SECTIONS = (
    "1. **Headline** — one sentence naming the slowest group and the overall reassignment rate.",
    "2. **Duration by {dimension}** — a markdown table with columns: {dimension} | Cases | "
    "Mean | Median. Sort by mean duration, slowest first.",
    "3. **Churn** — how reassignment and reopen counts are distributed, and which share of "
    "cases is never reassigned and never reopened.",
    "4. **What stands out** — two to four bullets, each tied to a number that appears above.",
)


def _hours(seconds: float | None) -> float | None:
    """Precompute hours so the report never depends on the model doing arithmetic."""
    return None if seconds is None else round(seconds / 3600, 1)


def _report_payload(
    dimension: str, window_from: datetime | None, window_to: datetime | None
) -> dict[str, Any]:
    """Run every aggregation the report needs, once, against one shared scope."""
    window = {
        "timestamp_from": window_from.isoformat() if window_from else None,
        "timestamp_to": window_to.isoformat() if window_to else None,
        "note": "a window selects cases by their first event and never truncates a case",
    }
    duration = processing_duration(
        DurationQuery(
            group_by=(dimension,),  # type: ignore[arg-type]
            timestamp_from=window_from,
            timestamp_to=window_to,
        ),
        _events(),
    )
    counters = {
        metric: counter_distribution(
            DistributionQuery(
                metric=metric,  # type: ignore[arg-type]
                timestamp_from=window_from,
                timestamp_to=window_to,
            ),
            _events(),
        )
        for metric in ("reassignment_count", "reopen_count")
    }
    return {
        "dataset": "incident management event log",
        "dimension": dimension,
        "window": window,
        "case_count": sum(row.count for row in duration.rows),
        "attribution": "each case is attributed by its final event",
        "duration_by_dimension": [
            {
                "value": row.group[dimension],
                "cases": row.count,
                "mean_duration_seconds": row.mean_duration_seconds,
                "median_duration_seconds": row.median_duration_seconds,
                "mean_duration_hours": _hours(row.mean_duration_seconds),
                "median_duration_hours": _hours(row.median_duration_seconds),
            }
            for row in duration.rows
        ],
        "counters": {
            metric: {
                "case_count": result.case_count,
                "zero_share": next((row.share for row in result.rows if row.value == 0), 0.0),
                "max_value": max((row.value for row in result.rows), default=0),
                "distribution": [
                    {"value": row.value, "cases": row.count, "share": row.share}
                    for row in result.rows
                ],
            }
            for metric, result in counters.items()
        },
    }


def _report_scope(
    dimension: str, timestamp_from: str | None, timestamp_to: str | None, audience: str
) -> tuple[str, datetime | None, datetime | None, str]:
    """Validate the four report arguments the prompt and the sampling tool share.

    Raises `ToolError`, which the tool path returns verbatim to the model; the prompt
    path re-raises it as `MCPError` because the prompt layer discards other messages.
    """
    if dimension not in DIMENSIONS:
        raise ToolError(f"dimension must be one of {list(DIMENSIONS)}; got {dimension!r}")
    if audience not in REPORT_AUDIENCES:
        raise ToolError(f"audience must be one of {sorted(REPORT_AUDIENCES)}; got {audience!r}")
    window_from = _parse_timestamp(timestamp_from, "timestamp_from")
    window_to = _parse_timestamp(timestamp_to, "timestamp_to")
    if window_from and window_to and window_from > window_to:
        raise ToolError("timestamp_from must not be later than timestamp_to")
    return dimension, window_from, window_to, audience


def _report_brief(dimension: str, audience: str, payload: dict[str, Any]) -> str:
    """The instructions the report must follow, whoever ends up writing it.

    One text, two deliveries: the prompt hands it to the user's own model, and the
    sampling tool sends it to the client's model. They cannot drift apart.
    """
    window = payload["window"]
    scope = (
        "the full event log"
        if window["timestamp_from"] is None and window["timestamp_to"] is None
        else (
            f"cases opened between {window['timestamp_from'] or 'the first event'} "
            f"and {window['timestamp_to'] or 'the last event'}"
        )
    )
    sections = "\n".join(section.format(dimension=dimension) for section in REPORT_SECTIONS)
    return (
        f"Write an incident management report on {scope}, broken down by {dimension}, "
        f"for {REPORT_AUDIENCES[audience]}.\n\n"
        f"Use markdown with these sections, in this order:\n\n{sections}\n\n"
        "Rules:\n"
        "- Every number must come from the attached data. Do not estimate, extrapolate, "
        "or call the server's tools to re-derive what is already attached.\n"
        "- Durations are given in both seconds and hours; quote hours (or days) and never "
        "convert by hand.\n"
        "- Shares are fractions of the cases in scope — render them as percentages to one "
        "decimal place.\n"
        "- Each case is attributed by its final event, so a case escalated mid-life counts "
        "under the value it ended with. Say so once if it affects a claim.\n"
        "- An empty string as a dimension value means the attribute was missing on the "
        'final event; report it as "(missing)" rather than dropping the row.\n'
        "- Close with no commentary beyond the sections above."
    )


@server.prompt(
    title="Incident report",
    description=(
        "Draft a formatted incident report for one dimension (priority, issue_type or "
        "report_channel) over an optional time window. The server runs the aggregations "
        "and embeds the results in the prompt, so the model writes the report over fixed "
        "numbers instead of calling the tools and re-deriving them."
    ),
)
def incident_report(
    dimension: str = "priority",
    timestamp_from: str | None = None,
    timestamp_to: str | None = None,
    audience: str = "operations",
) -> list[Message]:
    """Report brief plus the aggregated data it must be written from.

    Arguments arrive as strings from the client, so each one is validated here and a
    bad value comes back as an `INVALID_PARAMS` protocol error naming the alternatives
    — the prompt layer would otherwise replace the message with a generic
    "Error rendering prompt".
    """
    try:
        dimension, window_from, window_to, audience = _report_scope(
            dimension, timestamp_from, timestamp_to, audience
        )
    except ToolError as exc:
        raise MCPError(INVALID_PARAMS, str(exc)) from exc
    payload = _report_payload(dimension, window_from, window_to)
    brief = _report_brief(dimension, audience, payload)
    return [
        UserMessage(brief),
        UserMessage(
            {
                "type": "resource",
                "resource": {
                    "uri": f"data://incident-management/report/{dimension}",
                    "mimeType": "application/json",
                    "text": json.dumps(payload, indent=2),
                },
            }
        ),
    ]


REPORT_SYSTEM_PROMPT = (
    "You are a reporting assistant for an incident management event log. You return the "
    "report itself: markdown, no preamble, no sign-off, no offer of further help. Every "
    "figure you state must appear in the JSON data you are given — if the data does not "
    "support a claim, leave the claim out."
)

REPORT_MAX_TOKENS = 1500


def _report_data(
    dimension: str = "priority",
    timestamp_from: str | None = None,
    timestamp_to: str | None = None,
    audience: str = "operations",
) -> dict[str, Any]:
    """Resolver: the aggregation the sampling request and the tool result both need.

    Resolvers are memoised for the duration of one call, so the dataset is aggregated
    once even though two parameters depend on this. It also validates every argument,
    before anything is sent to the client's model.
    """
    dimension, window_from, window_to, _ = _report_scope(
        dimension, timestamp_from, timestamp_to, audience
    )
    return _report_payload(dimension, window_from, window_to)


def _sampled_report(
    payload: Annotated[dict[str, Any], Resolve(_report_data)],
    dimension: str = "priority",
    audience: str = "operations",
) -> Sample:
    """Resolver: ask the client's model to write the report, and hand back its answer.

    Returning `Sample` makes the framework issue `sampling/createMessage` before the
    tool body runs — the server borrows the caller's model rather than holding an API
    key of its own. The brief is the same text the `incident_report` prompt returns.
    """
    brief = _report_brief(dimension, audience, payload)
    return Sample(
        messages=[
            SamplingMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"{brief}\n\nData (JSON):\n{json.dumps(payload, indent=2)}",
                ),
            )
        ],
        max_tokens=REPORT_MAX_TOKENS,
        system_prompt=REPORT_SYSTEM_PROMPT,
        temperature=0.2,  # a report, not prose: same data should give the same wording
        model_preferences=ModelPreferences(
            intelligence_priority=0.8, speed_priority=0.3, cost_priority=0.2
        ),
    )


@server.tool(
    title="Write the incident report",
    description=(
        "Return a finished markdown incident report for one dimension (priority, "
        "issue_type or report_channel) over an optional time window. The server "
        "aggregates the data and asks *your* model to write it up (MCP sampling), so "
        "this needs the client's sampling capability; without it, call "
        "case_processing_duration and counter_value_distribution and write the report "
        "yourself."
    ),
)
def write_incident_report(
    dimension: str = "priority",
    timestamp_from: str | None = None,
    timestamp_to: str | None = None,
    audience: str = "operations",
    *,
    payload: Annotated[dict[str, Any], Resolve(_report_data)],
    draft: Annotated[CreateMessageResult, Resolve(_sampled_report)],
) -> dict[str, Any]:
    """Return the sampled report with the scope and the model that produced it.

    `payload` and `draft` are filled by resolvers, not by the caller: neither appears
    in the tool's input schema.
    """
    if draft.content.type != "text":
        raise ToolError(
            f"sampling returned {draft.content.type!r} content; the report needs text — "
            "the client's model or sampling handler is misconfigured"
        )
    return {
        "report": draft.content.text,
        "model": draft.model,
        "stop_reason": draft.stop_reason,
        "truncated": draft.stop_reason == "maxTokens",
        "scope": {
            "dimension": payload["dimension"],
            "case_count": payload["case_count"],
            "timestamp_from": payload["window"]["timestamp_from"],
            "timestamp_to": payload["window"]["timestamp_to"],
            "audience": audience,
        },
    }


def main() -> None:
    """Serve over stdio. The dataset loads on the first tool call, not at startup."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
