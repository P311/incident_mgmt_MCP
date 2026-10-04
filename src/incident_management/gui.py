"""JSON API bridge between the React GUI (`web/`) and the incident-management MCP server.

This module is a plain MCP *client*: it spawns the server as a subprocess and
talks JSON-RPC to it over stdio, the same way `scripts/call_tool.py` does. It
never imports `dataset.py` or `query.py` directly, so it stays independent of
the server's internals and would keep working against any server that speaks
the same tool/prompt contract. The React app in `web/` is an equally
independent module: it only ever talks to the HTTP routes below.

There is no model behind this GUI, so `write_incident_report` (which needs
MCP sampling) is exposed but reported as unavailable when called; the
`incident_report` prompt view gives a data-backed brief without needing one.

Run it with:
    .venv/bin/python -m incident_management.gui
or, once installed:
    incident-management-gui

In development, run the React app separately (`cd web && npm run dev`) — its
dev server proxies `/api/*` to this process. In production, `npm run build`
first and this process serves the built `web/dist` directly.
"""

from __future__ import annotations

import argparse
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

logger = logging.getLogger("incident-management-gui")

DEFAULT_SERVER_COMMAND = "incident-management-mcp"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIST_PATH = PROJECT_ROOT / "web" / "dist"
DEV_SERVER_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


class ToolCallError(RuntimeError):
    """Raised with the server's own error text when a tool call comes back as an error."""


class MCPClient:
    """One MCP session to the incident-management server, held for the GUI's lifetime.

    A fresh session per request would re-run the server's handshake (and its lazy
    dataset load) every time, so the GUI keeps one subprocess and one session open
    for as long as the web server runs.
    """

    def __init__(self, command: str, args: list[str]) -> None:
        self._params = StdioServerParameters(command=command, args=args)
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None
        self.server_name: str | None = None
        self.server_version: str | None = None

    async def start(self) -> None:
        read, write = await self._stack.enter_async_context(stdio_client(self._params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        init = await self.session.initialize()
        self.server_name = init.server_info.name
        self.server_version = init.server_info.version
        logger.info("connected to %s v%s", self.server_name, self.server_version)

    async def stop(self) -> None:
        await self._stack.aclose()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self.session is not None, "MCPClient.start() was not called"
        result = await self.session.call_tool(name, arguments)
        if result.is_error:
            message = "".join(block.text for block in result.content if block.type == "text")
            raise ToolCallError(message or f"{name} failed with no message")
        return result.structured_content or {}

    async def get_prompt(self, name: str, arguments: dict[str, str]) -> dict[str, Any]:
        assert self.session is not None, "MCPClient.start() was not called"
        result = await self.session.get_prompt(name, arguments)
        texts: list[str] = []
        payload: Any = None
        for message in result.messages:
            content = message.content
            if getattr(content, "type", None) == "text":
                texts.append(content.text)
            elif getattr(content, "type", None) == "resource":
                payload = json.loads(content.resource.text)
        return {"brief": "\n\n".join(texts), "payload": payload}


def _clean(value: str | None) -> str | None:
    """Blank form fields become None, not an empty-string filter or timestamp."""
    return value.strip() if value and value.strip() else None


def _report_arguments(body: dict[str, Any]) -> dict[str, str]:
    """`get_prompt` and the report tool take string arguments; omit the unset ones."""
    arguments = {
        "dimension": body.get("dimension") or "priority",
        "audience": body.get("audience") or "operations",
    }
    for field in ("timestamp_from", "timestamp_to"):
        value = _clean(body.get(field))
        if value is not None:
            arguments[field] = value
    return arguments


async def dev_notice(request: Request) -> JSONResponse:
    """Served at `/` only when `web/dist` hasn't been built yet."""
    return JSONResponse(
        {
            "service": "incident-management-gui API",
            "note": (
                "web/dist was not found. For development run the React app separately "
                "(cd web && npm install && npm run dev) — its dev server proxies /api to "
                "this process. For production, run `npm run build` in web/ first and this "
                "process will serve the built app directly."
            ),
        }
    )


async def health(request: Request) -> JSONResponse:
    client: MCPClient = request.app.state.mcp_client
    return JSONResponse({"server_name": client.server_name, "server_version": client.server_version})


async def dimension_values(request: Request) -> JSONResponse:
    client: MCPClient = request.app.state.mcp_client
    dimension = request.path_params["dimension"]
    try:
        data = await client.call_tool("list_dimension_values", {"dimension": dimension})
    except ToolCallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(data)


async def duration(request: Request) -> JSONResponse:
    client: MCPClient = request.app.state.mcp_client
    body = await request.json()
    arguments = {
        "group_by": body.get("group_by") or [],
        "priority": body.get("priority") or [],
        "issue_type": body.get("issue_type") or [],
        "report_channel": body.get("report_channel") or [],
        "timestamp_from": _clean(body.get("timestamp_from")),
        "timestamp_to": _clean(body.get("timestamp_to")),
    }
    try:
        data = await client.call_tool("case_processing_duration", arguments)
    except ToolCallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(data)


async def counters(request: Request) -> JSONResponse:
    client: MCPClient = request.app.state.mcp_client
    body = await request.json()
    arguments = {
        "metric": body.get("metric", "reassignment_count"),
        "priority": body.get("priority") or [],
        "issue_type": body.get("issue_type") or [],
        "report_channel": body.get("report_channel") or [],
        "timestamp_from": _clean(body.get("timestamp_from")),
        "timestamp_to": _clean(body.get("timestamp_to")),
    }
    try:
        data = await client.call_tool("counter_value_distribution", arguments)
    except ToolCallError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(data)


async def report_brief(request: Request) -> JSONResponse:
    client: MCPClient = request.app.state.mcp_client
    body = await request.json()
    try:
        data = await client.get_prompt("incident_report", _report_arguments(body))
    except Exception as exc:  # bad dimension/date comes back as a protocol-level error
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(data)


async def report_written(request: Request) -> JSONResponse:
    client: MCPClient = request.app.state.mcp_client
    body = await request.json()
    try:
        data = await client.call_tool("write_incident_report", _report_arguments(body))
    except (ToolCallError, MCPError) as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "hint": (
                    "This GUI has no model of its own to satisfy MCP sampling. "
                    "Use the report brief above, or drive this server from a "
                    "sampling-capable MCP client instead."
                ),
            },
            status_code=409,
        )
    return JSONResponse(data)


def build_app(command: str = DEFAULT_SERVER_COMMAND, args: list[str] | None = None) -> Starlette:
    """Construct the Starlette app, wiring one MCPClient through its lifespan."""
    client = MCPClient(command, args or [])

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await client.start()
        try:
            yield
        finally:
            await client.stop()

    routes = [
        Route("/api/health", health),
        Route("/api/dimensions/{dimension}", dimension_values),
        Route("/api/duration", duration, methods=["POST"]),
        Route("/api/counters", counters, methods=["POST"]),
        Route("/api/report", report_brief, methods=["POST"]),
        Route("/api/report/write", report_written, methods=["POST"]),
    ]
    if WEB_DIST_PATH.is_dir():
        routes.append(Mount("/", app=StaticFiles(directory=WEB_DIST_PATH, html=True), name="web"))
    else:
        routes.append(Route("/", dev_notice))

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=DEV_SERVER_ORIGINS,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        ],
        lifespan=lifespan,
    )
    app.state.mcp_client = client
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--server-command",
        default=DEFAULT_SERVER_COMMAND,
        help=f"MCP server executable to spawn (default: {DEFAULT_SERVER_COMMAND})",
    )
    parser.add_argument(
        "--server-arg",
        action="append",
        default=[],
        dest="server_args",
        help="extra argument for the MCP server command (repeatable)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import uvicorn

    app = build_app(args.server_command, args.server_args)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
