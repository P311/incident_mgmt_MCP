# Incident Management Event Log

An MCP server over a realistic IT incident-management event log (242,900 events, 31,588 tickets). It exposes deterministic aggregation tools, a BM25 keyword search tool, a report prompt, and a report tool that borrows the client's model through MCP sampling — plus the small Python SDK underneath and an optional browser console.

The design rule throughout: **the server does the arithmetic, the model does the writing.** Every number a model sees comes from a tested, deterministic aggregation, never from the model re-deriving it.

## Setup

```bash
git clone <this repo> && cd <repo>
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

The dataset ships in the repo as `data/incident_event_log.csv.zip` (2.2 MB) and is read straight from the zip, so there is no download step.

Validate the deployment:

```bash
.venv/bin/python scripts/validate_dataset.py
```

## Tests

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest              # whole suite, ~10s
.venv/bin/python -m pytest -m "not slow"  # skip the checks that load the full CSV
```

Four layers, in `tests/`:

| File | Covers |
| --- | --- |
| `test_dataset.py` | Schema contract, blank-activity rows, day-first dates, dtypes, derived reassignment/reopen counters, event ordering |
| `test_query.py` | Aggregation semantics — final-event attribution, filter/group agreement, counter rule, windows |
| `test_search.py` | BM25 tokenizing, indexing and ranking |
| `test_mcp_server.py` | Handshake, tool schemas, dispatch and error messages, the report prompt, the sampling round-trip, over the real protocol |

Most tests run against the six-case fixture in `tests/conftest.py`, where every expected number can be checked by hand; it deliberately includes an escalated case, a downgraded case, a case with a missing final issue_type, and a case outside the March window. Tests marked `slow` assert totals against the real 242,900-row file.

Two things worth knowing before extending the suite:

- **MCP errors must be asserted through the protocol.** `server.call_tool()` *raises*; it is the dispatch layer that converts a `ToolError` into the `is_error` result a client sees. `tests/test_mcp_server.py` therefore drives a real `ClientSession` over in-memory streams rather than calling the tool functions directly — no subprocess, same exchange.
- **The suite is checked against regressions it should catch.** Reverting final-event attribution and moving filtering back to the event layer both fail multiple tests, and swapping a `ToolError` for a `ValueError` fails others.

### Use the SDK

```python
from incident_management import load_event_log, to_pm4py

events = load_event_log()
pm4py_log = to_pm4py(events)
```

## Deterministic query contract

Tool 1 is exposed as `processing_duration(DurationQuery)`. Its input is fixed:

- `group_by`: one or more of `priority`, `issue_type`, `report_channel`
- `priority`: optional values from the `priority` column (`High`, `Medium`, `Low`)
- `issue_type`: optional values from the `issue_type` column (`Bug`, `Incident`, `Feature Request`, `Maintenance`, `Performance Issue`, `Service Request`, `Technical Issue`)
- `report_channel`: optional values from the `report_channel` column (`App`, `Email`, `Phone`, `Website`)
- `timestamp_from` / `timestamp_to`: optional inclusive ISO-8601 bounds on case start time

A case is attributed by its **final** event: if a case's priority, issue_type or report_channel changes mid-life, it is reported under the value it ended with (in this dataset these three are in fact constant per case, but the rule is still enforced generically). Filters and `group_by` share this one definition, so filtering to `High` selects exactly the cases that `group_by=("priority",)` reports as high priority (6,466 of them). Filters apply to case-level attributes after aggregation, so they never truncate a case's duration.

The fixed output is `DurationResult(metric, duration_unit, timestamp_from, timestamp_to, rows)`. Each row has `group`, `count`, `mean_duration_seconds`, and `median_duration_seconds`. Duration is the difference between the first and last event of each case; a time window selects cases by their first event and does not truncate their duration.

Example:

```python
from incident_management import DurationQuery, processing_duration

result = processing_duration(
	DurationQuery(group_by=("priority", "issue_type"), priority=("High",))
)
```

## Counter distribution contract

Tool 2 is exposed as `counter_distribution(DistributionQuery)`. Its input is fixed:

- `metric`: either `reassignment_count` or `reopen_count`
- `priority` / `issue_type` / `report_channel`: optional case-level value filters, same as Tool 1 (final-event attribution)
- `timestamp_from` / `timestamp_to`: optional inclusive ISO-8601 bounds on case start time

The fixed output is `DistributionResult(metric, case_count, timestamp_from, timestamp_to, rows)`. Each row has `value` (the counter value), `count` (cases with that value), and `share` (`count / case_count`, rounded to 6 decimals). Rows are sorted ascending by `value`, and counter values with no cases are omitted rather than emitted as zero rows.

**Counting rule.** This event log carries no raw reassignment/reopen counter columns at all — only an 18-value `Event` activity per row (`Ticket created`, `... escalates to level 2/3 support`, `... rejected by level 1 support`, `Ticket reopened by customer`, `Ticket closed`, etc.). `dataset.py` derives both counters at load time from that activity sequence: `reassignment_count` increments on any handoff to a different level/agent *after* the initial assignment (escalations, follow-up assignments, rejections back down); `reopen_count` increments on `Ticket reopened by customer`. Both are running counters carried on every event, so each case contributes exactly one observation: its maximum counter value, which is also its final value (verified non-decreasing within every case).

```python
from incident_management import DistributionQuery, counter_distribution

result = counter_distribution(DistributionQuery(metric="reassignment_count"))
```

Across all 31,588 cases, 34.3% are never reassigned (by the derived rule above) and 96.2% are never reopened; reassignment reaches a maximum of 5 and reopen a maximum of 1.

## Keyword search

`keyword_search` ranks cases against a free-text query using Okapi BM25 (`src/incident_management/search.py`, no external search dependency). The sentence is split into words and scored against one document per case, built by concatenating `short_description`, `issue_type`, `priority`, `report_channel`, `reporter` and `resolver` (final-event attribution again). `short_description` only takes eight distinct values (`Application crash`, `Server outage`, `Unable to login`, `Password reset needed`, `Printer not working`, `UI glitch`, `Data loss issue`, `New feature request`) rather than free-form prose per ticket, but they're real English words, so a query like `"password reset"` or `"server down"` returns meaningfully relevant cases instead of matching anonymized codes.

Unlike the two query tools, there's no top-level SDK function for it — call it as the MCP tool `keyword_search(sentence, top_k=25)`, which returns the matched terms, total case count, and up to `top_k` hits with their BM25 score and the field values they matched on. The underlying pieces (`tokenize`, `BM25Index`, `build_index`, `search_incidents`) live in `src/incident_management/search.py` if you want to call them directly.

## MCP server

The query contracts and search are exposed over MCP (stdio transport) in `src/incident_management/mcp_server.py`, together with a report prompt and a report tool that samples the client's model:

```bash
.venv/bin/python -m incident_management.mcp_server
```

Registered tools:

| Tool | Purpose |
| --- | --- |
| `list_dimension_values` | Exact values for `priority` / `issue_type` / `report_channel`, with case counts |
| `case_processing_duration` | Tool 1 — duration aggregates by dimension |
| `counter_value_distribution` | Tool 2 — `reassignment_count` / `reopen_count` distribution |
| `keyword_search` | BM25 keyword search over incidents |
| `write_incident_report` | A finished report, written by the client's own model via sampling |

Registered prompts:

| Prompt | Purpose |
| --- | --- |
| `incident_report` | A report brief with the aggregated data already attached |

Register it with an MCP client (e.g. `claude_desktop_config.json`). Replace `/path/to/repo` with
your checkout. If Claude Desktop and this project are both on the same Linux (or macOS) machine:

```json
{
  "mcpServers": {
    "incident-management": {
      "command": "/path/to/repo/.venv/bin/python",
      "args": ["-m", "incident_management.mcp_server"],
      "cwd": "/path/to/repo"
    }
  }
}
```

If Claude Desktop runs on Windows but the project lives in WSL, the command
must be a Windows-executable — `wsl.exe` — that shells into the distro, since Windows can't run a
Linux binary directly:

```json
{
  "mcpServers": {
    "incident-management": {
      "command": "wsl.exe",
      "args": [
        "-d", "Ubuntu",
        "--cd", "/path/to/repo",
        "--",
        "/path/to/repo/.venv/bin/python",
        "-m", "incident_management.mcp_server"
      ]
    }
  }
}
```

The config file itself is not always at the classic `%APPDATA%\Claude\claude_desktop_config.json`
either — some builds (e.g. Microsoft Store installs) virtualize it under
`%LOCALAPPDATA%\Packages\<package name>\LocalCache\Roaming\Claude\claude_desktop_config.json`.
Fully quit and relaunch Claude Desktop after editing either file for the change to take effect.

Two constraints matter when editing this server:

- **stdout belongs to the JSON-RPC stream.** Diagnostics go to stderr (`logging` is configured that way); a stray `print()` corrupts the protocol.
- **Raise `ToolError` for bad input, not `ValueError`.** The SDK preserves a `ToolError` message so the model can correct itself, but replaces any other exception's text with a generic one to avoid leaking internals.

The 242,900-row dataset loads lazily on the first tool call or prompt render, so the `initialize` handshake returns immediately. Filter values are validated against the dataset — an unknown value such as `"high"` returns an error naming the valid forms rather than a silently empty result.

## Report prompt

`incident_report` turns the query contracts into a written report. It is a *prompt*, not a tool: the client shows it as a slash command or menu entry the user picks, and the server answers with the messages that seed the conversation.

| Argument | Default | Meaning |
| --- | --- | --- |
| `dimension` | `priority` | Breakdown dimension — `priority`, `issue_type` or `report_channel` |
| `timestamp_from` / `timestamp_to` | none | Inclusive ISO-8601 bounds on case start time |
| `audience` | `operations` | `operations` (numbers in the prose) or `executive` (one headline, whole days) |

All four are optional, so the bare prompt reports priority over the whole log.

**The server does the arithmetic, the model does the writing.** The handler runs `processing_duration` and both `counter_distribution` metrics itself over one shared scope, then returns two messages: the report brief, and the results as an embedded `application/json` resource. The model formats numbers it was handed rather than calling the tools and re-deriving them, and durations arrive in seconds *and* hours so no conversion is left to it. The brief fixes the four sections (headline, duration table, churn, what stands out), forbids numbers that are not in the payload, and carries the final-event attribution rule so the report cannot misstate it.

```
prompts/get incident_report {"dimension": "report_channel", "timestamp_from": "2023-06-01"}
  -> user message: the brief (sections, formatting rules, audience)
  -> user message: data://incident-management/report/report_channel (application/json)
```

**Prompt errors need `MCPError`, not `ToolError`.** The prompt layer replaces any ordinary exception's text with `Error rendering prompt <name>`, so a bad `dimension` or an unparseable date is raised as `MCPError(INVALID_PARAMS, ...)` to keep the message that names the valid alternatives.

## Sampling: the server borrows the client's model

`write_incident_report` returns the report already written. The server has no API key and no model of its own — it asks the *client* for a completion over MCP sampling (`sampling/createMessage`), which is the mirror image of the prompt: the prompt hands work to the caller's model and stops, sampling calls the caller's model and keeps going.

It takes the same four arguments as the prompt and returns:

| Field | Meaning |
| --- | --- |
| `report` | The markdown the client's model wrote |
| `model` | Which model the client actually used |
| `stop_reason` / `truncated` | `truncated` is true when the answer hit the 1500-token cap |
| `scope` | Dimension, window, case count and audience the report was written from |

**Two resolvers, one aggregation.** The work is wired as a resolver DAG rather than done in the tool body:

```
_report_data   ──> the aggregated payload (validates arguments, hits pandas once)
     │
     └──> _sampled_report ──> Sample(...) ──> sampling/createMessage ──> the client's model
                    │
     ┌──────────────┴──────────────┐
     v                             v
write_incident_report(payload=..., draft=...)
```

`payload` and `draft` are `Annotated[..., Resolve(...)]` parameters, so the framework fills them before the body runs and keeps them out of the input schema — the model calling the tool sees only the four real arguments. Resolvers are memoised per call, so `_report_data` runs once even though both the sampling request and the tool result depend on it.

Three consequences worth knowing:

- **The brief is shared with the prompt.** Both paths build it with `_report_brief`, so the two report surfaces cannot drift apart; a test asserts the sampled request and the prompt render the same text.
- **Validation happens in the resolver, before the model is asked.** A bad `dimension` fails the call without spending a token.
- **Sampling is a client capability, not a given.** A client that declares no `sampling` capability gets a protocol error naming the missing capability, which is why the tool description tells the model to fall back to the two query tools and write the report itself.

## Dataset schema

The loader reads `data/incident_event_log.csv.zip` (one `;`-separated CSV inside, 242,900 events after one malformed row is dropped, 31,588 cases, 17 activities) and normalizes its headers to snake_case, which already gives the three process-mining names the loader relies on:

| column | meaning |
| --- | --- |
| `case_id` | the ticket number, e.g. `INC0001` |
| `event` | one of 18 activities: `Ticket created` → `... assigned to level 1 support` → `WIP - level 1 support` → (optionally escalate to level 2/3, get rejected back down, or get reopened) → `Ticket closed` |
| `timestamp` | per-event, `%d/%m/%Y %H:%M` |
| `priority` | `High` / `Medium` / `Low`, constant per case |
| `issue_type` | `Bug`, `Incident`, `Feature Request`, `Maintenance`, `Performance Issue`, `Service Request`, `Technical Issue` — constant per case |
| `report_channel` | `App`, `Email`, `Phone`, `Website` — constant per case |
| `short_description` | one of 8 fixed phrases (see [Keyword search](#keyword-search)) — constant per case |
| `reporter` / `resolver` | who opened the ticket / who is handling this event (`resolver` is empty until assignment) |
| `variant` | which of the 13 process variants the case follows |
| `customer_satisfaction` | 1–5, on the case's closing event |
| `reassignment_count` / `reopen_count` | **derived**, not raw columns — see [Counter distribution contract](#counter-distribution-contract) |

A single row with a blank `Event` is dropped at load time (one such row exists in the source file; it cannot be placed in a case's process flow). Sorting is by `case_id` then `timestamp`, stable, so any tied timestamps keep the source file's order.

The zip is the unmodified Kaggle download. The dataset is *Process Mining Event Log - Incident Management* by Alberto P on Kaggle, redistributed here under its MIT license: <https://www.kaggle.com/datasets/albertopmd/process-mining-event-log-incident-management>. It was chosen over the better-known UCI incident log because the UCI data is fully anonymized (`Category 42`, `Symptom 491`, ...) and has no free text to back a keyword search tool.

## Browser GUI

Two independent modules give the MCP server a browser front end:

- `src/incident_management/gui.py` — a small JSON API. It is a plain MCP *client*: it spawns `incident-management-mcp` as a subprocess and speaks the same protocol `scripts/call_tool.py` does, then exposes the query/report tools and the `incident_report` prompt as HTTP routes (`/api/...`).
- `web/` — a standalone React app (Vite) that only ever talks to that HTTP API. It never imports Python, so it could be pointed at any server implementing the same routes.

Development (two processes, hot reload on the frontend):

```bash
.venv/bin/incident-management-gui          # API on :8000
cd web && npm install && npm run dev       # UI on :5173, proxies /api to :8000
```

Production (one process):

```bash
cd web && npm install && npm run build     # writes web/dist
cd .. && .venv/bin/incident-management-gui # serves web/dist and the API together on :8000
```

The console covers `list_dimension_values`, `case_processing_duration`, `counter_value_distribution`, and the `incident_report` prompt (brief + attached data, no model needed). It also exposes `write_incident_report`, but since the browser has no model of its own to satisfy MCP sampling, that call is expected to report itself unavailable — the UI surfaces the server's own error message and points back at the brief. `keyword_search` is not yet wired into the GUI (no `/api/search` route or search panel); use `scripts/call_tool.py keyword_search '{"sentence": "..."}'`, Claude Desktop, or another MCP client to exercise it today.

## License

Code is MIT-licensed (see `LICENSE`). The bundled dataset is MIT-licensed by its original author; see [Dataset schema](#dataset-schema) for the source.
