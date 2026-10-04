"""MCP layer: the handshake, the advertised tool surface, and tool dispatch.

Everything here goes through the real protocol over in-memory streams — the same
`initialize` / `tools/list` / `tools/call` / `prompts/get` exchange a client performs,
without the cost of a subprocess. That matters for the error tests in particular:
calling `server.call_tool()` directly *raises*, and it is the dispatch layer that turns
a raised `ToolError` into the `is_error` result a client actually receives. It matters
for sampling too: `write_incident_report` asks the client for a completion mid-call, so
the test client answers with `FakeModel` and then asserts on what it was asked.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import ANY

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CreateMessageResult, ImageContent, TextContent

from incident_management.mcp_server import server

TOOL_NAMES = {
    "list_dimension_values",
    "case_processing_duration",
    "counter_value_distribution",
    "keyword_search",
    "write_incident_report",
}
PROMPT_NAMES = {"incident_report"}


def via_protocol(request, sampling_callback=None) -> dict[str, Any]:
    """Connect a client to the server in-process and run `request(session)`.

    Returns the `initialize` result alongside whatever the request produced, so a
    test can assert on the handshake, the call, or both.
    """
    box: dict[str, Any] = {}

    async def main() -> None:
        async with create_client_server_memory_streams() as (
            (client_read, client_write),
            (server_read, server_write),
        ):
            async with anyio.create_task_group() as tg:

                async def serve() -> None:
                    await server._lowlevel_server.run(
                        server_read,
                        server_write,
                        server._lowlevel_server.create_initialization_options(),
                    )

                tg.start_soon(serve)
                async with ClientSession(
                    client_read, client_write, sampling_callback=sampling_callback
                ) as session:
                    box["init"] = await session.initialize()
                    box["value"] = await request(session)
                tg.cancel_scope.cancel()

    try:
        asyncio.run(main())
    except BaseExceptionGroup as group:
        raise _single_cause(group) from None
    return box


def _single_cause(error: BaseException) -> BaseException:
    """Unwrap the task group's `ExceptionGroup` down to the failure a client would see.

    A `prompts/get` error arrives as a raised `MCPError`, but it is raised inside the
    task group running the server, so it reaches the test wrapped in nested groups.
    """
    while isinstance(error, BaseExceptionGroup):
        if len(error.exceptions) != 1:
            return error
        error = error.exceptions[0]
    return error


async def _nothing(session):
    return None


def handshake():
    return via_protocol(_nothing)["init"]


def list_tools():
    return via_protocol(lambda session: session.list_tools())["value"].tools


def call(name: str, arguments: dict, sampling_callback=None):
    return via_protocol(
        lambda session: session.call_tool(name, arguments), sampling_callback=sampling_callback
    )["value"]


def text_of(result) -> str:
    return "".join(block.text for block in result.content if block.type == "text")


class TestHandshake:
    def test_reports_server_identity(self):
        init = handshake()
        assert init.server_info.name == "incident-management"
        assert init.server_info.version == "0.1.0"

    def test_negotiates_a_protocol_version(self):
        assert handshake().protocol_version

    def test_advertises_tool_capability(self):
        assert handshake().capabilities.tools is not None

    def test_instructions_steer_the_client_away_from_guessing_values(self):
        instructions = handshake().instructions or ""
        assert "list_dimension_values" in instructions
        assert "High" in instructions


class TestToolSurface:
    def test_all_tools_are_listed(self):
        assert {tool.name for tool in list_tools()} == TOOL_NAMES

    def test_required_arguments(self):
        schemas = {tool.name: tool.input_schema for tool in list_tools()}
        assert schemas["list_dimension_values"]["required"] == ["dimension"]
        assert schemas["case_processing_duration"]["required"] == ["group_by"]
        assert schemas["counter_value_distribution"]["required"] == ["metric"]
        assert schemas["keyword_search"]["required"] == ["sentence"]

    def test_filters_are_optional(self):
        schema = {tool.name: tool.input_schema for tool in list_tools()}["case_processing_duration"]
        assert {"priority", "issue_type", "report_channel", "timestamp_from", "timestamp_to"} <= set(
            schema["properties"]
        )
        assert "priority" not in schema.get("required", [])

    def test_every_tool_is_described(self):
        for tool in list_tools():
            assert tool.description and len(tool.description) > 40


class TestSuccessfulCalls:
    def test_list_dimension_values(self):
        result = call("list_dimension_values", {"dimension": "report_channel"})
        assert result.is_error is False
        values = [row["value"] for row in result.structured_content["values"]]
        assert values == ["App", "Email", "Phone", "Website"]

    def test_duration_grouping(self):
        body = call("case_processing_duration", {"group_by": ["priority"]}).structured_content
        assert sum(row["count"] for row in body["rows"]) == 31588
        assert body["duration_unit"] == "seconds"

    def test_counter_distribution(self):
        body = call("counter_value_distribution", {"metric": "reassignment_count"}).structured_content
        assert body["case_count"] == 31588
        assert sum(row["count"] for row in body["rows"]) == 31588

    def test_filters_and_window_are_applied(self):
        body = call(
            "case_processing_duration",
            {
                "group_by": ["priority"],
                "priority": ["High"],
                "timestamp_from": "2023-01-01",
                "timestamp_to": "2023-06-30",
            },
        ).structured_content
        assert len(body["rows"]) == 1
        assert body["rows"][0]["group"]["priority"] == "High"
        assert body["rows"][0]["count"] < 6466  # a partial window is a strict subset

    def test_keyword_search_ranks_matching_cases_first(self):
        body = call("keyword_search", {"sentence": "Server outage"}).structured_content
        assert body["query_terms"] == ["server", "outage"]
        assert body["case_count"] == 31588
        assert len(body["results"]) == 25  # default top_k
        assert body["results"][0]["fields"]["short_description"] == "Server outage"
        scores = [row["score"] for row in body["results"]]
        assert scores == sorted(scores, reverse=True)

    def test_keyword_search_top_k_is_respected(self):
        body = call("keyword_search", {"sentence": "Server outage", "top_k": 5}).structured_content
        assert len(body["results"]) == 5


class TestErrorsReachTheCaller:
    """Bad input must arrive as a readable message, not a generic failure.

    The SDK preserves the text of a `ToolError` but replaces any other exception's
    message with a bare "Error executing tool <name>", so these tests pin that the
    server raises the kind of error a model can act on.
    """

    @pytest.mark.parametrize(
        "name, arguments, expected",
        [
            ("list_dimension_values", {"dimension": "severity"}, "dimension must be one of"),
            ("counter_value_distribution", {"metric": "customer_satisfaction"}, "metric must be"),
            ("case_processing_duration", {"group_by": ["resolver"]}, "group_by must be"),
            ("case_processing_duration", {"group_by": []}, "group_by must be"),
            (
                "case_processing_duration",
                {"group_by": ["priority"], "timestamp_from": "March 2024"},
                "must be ISO-8601",
            ),
            ("keyword_search", {"sentence": ""}, "sentence must not be empty"),
            ("keyword_search", {"sentence": "outage", "top_k": 0}, "top_k must be between"),
            ("keyword_search", {"sentence": "outage", "top_k": 101}, "top_k must be between"),
            ("keyword_search", {"sentence": "!!!"}, "contains no searchable words"),
        ],
    )
    def test_message_is_actionable(self, name, arguments, expected):
        result = call(name, arguments)
        assert result.is_error is True
        assert expected in text_of(result)

    def test_unknown_filter_value_errors_instead_of_returning_empty(self):
        """'high' instead of 'High' must not look like "there are no such cases"."""
        result = call("case_processing_duration", {"group_by": ["priority"], "priority": ["high"]})
        assert result.is_error is True
        message = text_of(result)
        assert "unknown priority value(s)" in message
        assert "list_dimension_values" in message  # points at the fix

    def test_missing_required_argument_is_reported(self):
        result = call("counter_value_distribution", {})
        assert result.is_error is True
        assert "metric" in text_of(result)

    def test_valid_value_still_passes(self):
        result = call("case_processing_duration", {"group_by": ["priority"], "priority": ["Medium"]})
        assert result.is_error is False


def list_prompts():
    return via_protocol(lambda session: session.list_prompts())["value"].prompts


def get_prompt(name: str, arguments: dict | None = None):
    return via_protocol(lambda session: session.get_prompt(name, arguments or {}))["value"]


def prompt_text(result) -> str:
    return "".join(m.content.text for m in result.messages if m.content.type == "text")


def prompt_data(result) -> dict:
    """The JSON payload the server embeds for the model to write the report from."""
    resources = [m.content for m in result.messages if m.content.type == "resource"]
    assert len(resources) == 1
    return json.loads(resources[0].resource.text)


class TestPromptSurface:
    def test_prompt_capability_is_advertised(self):
        assert handshake().capabilities.prompts is not None

    def test_all_prompts_are_listed_with_a_title_and_description(self):
        assert {prompt.name for prompt in list_prompts()} == PROMPT_NAMES
        prompt = list_prompts()[0]
        assert prompt.title == "Incident report"
        assert len(prompt.description) > 40

    def test_every_argument_is_optional_so_the_prompt_runs_bare(self):
        prompt = {p.name: p for p in list_prompts()}["incident_report"]
        assert {a.name for a in prompt.arguments} == {
            "dimension",
            "timestamp_from",
            "timestamp_to",
            "audience",
        }
        assert not [a.name for a in prompt.arguments if a.required]


class TestReportPrompt:
    """The prompt ships the aggregation *with* the brief.

    The point of embedding the data is that the model formats numbers it was given
    rather than re-deriving them, so these tests pin both halves: the instructions
    the report must follow, and the payload those instructions refer to.
    """

    def test_defaults_to_priority_over_the_whole_log(self):
        data = prompt_data(get_prompt("incident_report"))
        assert data["dimension"] == "priority"
        assert data["window"] == {"timestamp_from": None, "timestamp_to": None, "note": ANY}
        assert data["case_count"] == 31588

    def test_payload_carries_the_duration_breakdown(self):
        data = prompt_data(get_prompt("incident_report", {"dimension": "report_channel"}))
        rows = data["duration_by_dimension"]
        assert [row["value"] for row in rows] == ["App", "Email", "Phone", "Website"]
        assert sum(row["cases"] for row in rows) == 31588

    def test_durations_come_precomputed_in_hours(self):
        """The model must never have to divide by 3600 itself."""
        for row in prompt_data(get_prompt("incident_report"))["duration_by_dimension"]:
            assert row["mean_duration_hours"] == pytest.approx(
                row["mean_duration_seconds"] / 3600, abs=0.05
            )

    def test_payload_carries_both_counter_distributions(self):
        counters = prompt_data(get_prompt("incident_report"))["counters"]
        assert set(counters) == {"reassignment_count", "reopen_count"}
        assert counters["reassignment_count"]["zero_share"] == pytest.approx(0.343, abs=0.001)
        assert counters["reopen_count"]["zero_share"] == pytest.approx(0.962, abs=0.001)
        assert counters["reassignment_count"]["max_value"] == 5

    def test_window_narrows_the_payload_and_the_brief(self):
        result = get_prompt(
            "incident_report", {"timestamp_from": "2023-01-01", "timestamp_to": "2023-06-30"}
        )
        data = prompt_data(result)
        assert data["window"]["timestamp_from"] == "2023-01-01T00:00:00"
        assert data["case_count"] < 31588
        assert "2023-01-01T00:00:00" in prompt_text(result)

    def test_brief_names_the_sections_and_the_dimension(self):
        text = prompt_text(get_prompt("incident_report", {"dimension": "issue_type"}))
        for heading in ("Headline", "Duration by issue_type", "Churn", "What stands out"):
            assert heading in text

    def test_brief_forbids_inventing_or_re_deriving_numbers(self):
        text = prompt_text(get_prompt("incident_report"))
        assert "Every number must come from the attached data" in text
        assert "final event" in text  # the attribution rule the report must respect

    def test_audience_changes_the_brief_but_not_the_data(self):
        ops = get_prompt("incident_report", {"audience": "operations"})
        exec_ = get_prompt("incident_report", {"audience": "executive"})
        assert prompt_data(ops) == prompt_data(exec_)
        assert prompt_text(ops) != prompt_text(exec_)
        assert "executive" in prompt_text(exec_)


class TestPromptErrorsReachTheCaller:
    """A prompt argument is typed by a human, so a bad one must say what to type instead.

    `get_prompt` wraps any plain exception as "Error rendering prompt <name>", which
    tells the caller nothing — the server raises `MCPError` so its own message survives.
    """

    @pytest.mark.parametrize(
        "arguments, expected",
        [
            ({"dimension": "severity"}, "dimension must be one of"),
            ({"audience": "board"}, "audience must be one of"),
            ({"timestamp_from": "March 2024"}, "must be ISO-8601"),
            (
                {"timestamp_from": "2024-05-01", "timestamp_to": "2023-03-01"},
                "must not be later than",
            ),
        ],
    )
    def test_message_is_actionable(self, arguments, expected):
        with pytest.raises(MCPError) as excinfo:
            get_prompt("incident_report", arguments)
        assert expected in str(excinfo.value)
        assert "Error rendering prompt" not in str(excinfo.value)


class FakeModel:
    """A client that answers `sampling/createMessage`, recording what it was asked.

    Standing in for the caller's LLM is the only way to exercise sampling in a test:
    the server does not hold a model, it borrows the client's.
    """

    def __init__(
        self,
        text: str = "# Incident report\n\nWritten by the client's model.",
        stop_reason: str = "endTurn",
        content=None,
    ):
        self.text = text
        self.stop_reason = stop_reason
        self.content = content
        self.requests: list[Any] = []

    async def __call__(self, context, params):
        self.requests.append(params)
        return CreateMessageResult(
            role="assistant",
            content=self.content or TextContent(type="text", text=self.text),
            model="fake-model-1",
            stop_reason=self.stop_reason,
        )

    @property
    def prompt_text(self) -> str:
        """The single user message the server sent."""
        assert len(self.requests) == 1
        return self.requests[0].messages[0].content.text


class TestSamplingSurface:
    def test_resolved_parameters_are_hidden_from_the_schema(self):
        """`payload` and `draft` are filled by resolvers, so the model must not see them."""
        schema = {tool.name: tool.input_schema for tool in list_tools()}["write_incident_report"]
        assert set(schema["properties"]) == {
            "dimension",
            "timestamp_from",
            "timestamp_to",
            "audience",
        }
        assert not schema.get("required")

    def test_description_names_the_fallback_for_clients_without_sampling(self):
        tool = {t.name: t for t in list_tools()}["write_incident_report"]
        assert "sampling" in tool.description
        assert "case_processing_duration" in tool.description


class TestSampledReport:
    def test_returns_what_the_client_model_wrote(self):
        model = FakeModel()
        body = call("write_incident_report", {}, model).structured_content
        assert body["report"] == model.text
        assert body["model"] == "fake-model-1"
        assert body["stop_reason"] == "endTurn"
        assert body["truncated"] is False

    def test_result_carries_the_scope_the_report_was_written_from(self):
        body = call(
            "write_incident_report",
            {"dimension": "report_channel", "timestamp_from": "2023-06-01", "audience": "executive"},
            FakeModel(),
        ).structured_content
        assert body["scope"] == {
            "dimension": "report_channel",
            "case_count": ANY,
            "timestamp_from": "2023-06-01T00:00:00",
            "timestamp_to": None,
            "audience": "executive",
        }
        assert body["scope"]["case_count"] < 31588

    def test_sampling_request_carries_the_brief_and_the_data(self):
        model = FakeModel()
        call("write_incident_report", {"dimension": "report_channel"}, model)
        sent = model.prompt_text
        assert "broken down by report_channel" in sent
        assert "Every number must come from the attached data" in sent
        data = json.loads(sent.split("Data (JSON):\n", 1)[1])
        assert data["dimension"] == "report_channel"
        assert sum(row["cases"] for row in data["duration_by_dimension"]) == 31588

    def test_sampling_request_is_shaped_for_a_factual_report(self):
        model = FakeModel()
        call("write_incident_report", {}, model)
        params = model.requests[0]
        assert "no preamble" in params.system_prompt
        assert params.max_tokens == 1500
        assert params.temperature == 0.2
        assert params.model_preferences.intelligence_priority == 0.8

    def test_the_prompt_and_the_sampled_request_share_one_brief(self):
        """The two report paths must not drift: same instructions, different delivery."""
        model = FakeModel()
        call("write_incident_report", {"dimension": "issue_type", "audience": "executive"}, model)
        brief = model.prompt_text.split("\n\nData (JSON):\n", 1)[0]
        prompt = get_prompt(
            "incident_report", {"dimension": "issue_type", "audience": "executive"}
        )
        assert brief == prompt_text(prompt)

    def test_the_dataset_is_aggregated_once_per_call(self, monkeypatch):
        """Two parameters depend on the data resolver; resolvers are memoised per call."""
        from incident_management import mcp_server

        calls = []
        original = mcp_server._report_payload
        monkeypatch.setattr(
            mcp_server,
            "_report_payload",
            lambda *args: (calls.append(args), original(*args))[1],
        )
        call("write_incident_report", {}, FakeModel())
        assert len(calls) == 1

    def test_a_truncated_answer_is_flagged_rather_than_hidden(self):
        body = call(
            "write_incident_report", {}, FakeModel(stop_reason="maxTokens")
        ).structured_content
        assert body["truncated"] is True
        assert body["stop_reason"] == "maxTokens"


class TestSamplingFailuresReachTheCaller:
    def test_bad_arguments_fail_before_the_model_is_asked(self):
        """Validation lives in the data resolver, so a bad value costs no tokens."""
        model = FakeModel()
        result = call("write_incident_report", {"dimension": "severity"}, model)
        assert result.is_error is True
        assert "dimension must be one of" in text_of(result)
        assert model.requests == []

    @pytest.mark.parametrize(
        "arguments, expected",
        [
            ({"audience": "board"}, "audience must be one of"),
            ({"timestamp_from": "March 2024"}, "must be ISO-8601"),
            (
                {"timestamp_from": "2024-05-01", "timestamp_to": "2023-03-01"},
                "must not be later than",
            ),
        ],
    )
    def test_argument_errors_are_actionable(self, arguments, expected):
        result = call("write_incident_report", arguments, FakeModel())
        assert result.is_error is True
        assert expected in text_of(result)

    def test_non_text_content_is_rejected(self):
        image = ImageContent(type="image", data="AA==", mimeType="image/png")
        result = call("write_incident_report", {}, FakeModel(content=image))
        assert result.is_error is True
        assert "sampling returned 'image' content" in text_of(result)

    def test_a_client_without_sampling_is_told_what_is_missing(self):
        """No `sampling_callback` means no declared capability; the SDK refuses the call."""
        with pytest.raises(MCPError) as excinfo:
            call("write_incident_report", {})
        assert "sampling capability" in str(excinfo.value)
