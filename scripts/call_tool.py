"""Send one tool call to the MCP server and print the response.

Usage: python scripts/call_tool.py <tool_name> ['<json arguments>']
       python scripts/call_tool.py --list
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(name: str | None, arguments: dict) -> int:
    params = StdioServerParameters(command="incident-management-mcp", args=[])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"# {init.server_info.name} v{init.server_info.version}", file=sys.stderr)

            if name is None:
                for tool in (await session.list_tools()).tools:
                    print(f"{tool.name}{sorted(tool.input_schema['properties'])}")
                return 0

            result = await session.call_tool(name, arguments)
            if result.is_error:
                print("".join(b.text for b in result.content if b.type == "text"))
                return 1
            print(json.dumps(result.structured_content, indent=2))
            return 0


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("--list", "-l"):
        sys.exit(asyncio.run(run(None, {})))
    arguments = json.loads(argv[1]) if len(argv) > 1 else {}
    sys.exit(asyncio.run(run(argv[0], arguments)))


if __name__ == "__main__":
    main()
