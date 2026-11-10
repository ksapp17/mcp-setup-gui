"""Generic MCP runtime: serves the tools described in a project.json.

Run:  uv run runtime.py --project C:\\full\\path\\to\\project.json

Nothing in here knows about weather. Add a tool to project.json,
restart Claude Desktop, and it shows up. No code changes.
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

import anyio
import httpx
import keyring
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

KEYCHAIN_SERVICE = "mcp-setup-gui"


def log(*parts):
    # stdout carries the MCP messages, so everything else goes to stderr.
    # Claude Desktop puts stderr in its MCP log for this server.
    print("[runtime]", *parts, file=sys.stderr)


# ---------------------------------------------------------------- project ---

def load_project(path_str):
    path = Path(path_str).resolve()
    project = json.loads(path.read_text(encoding="utf-8"))

    seen = set()
    for api in project["apis"]:
        if "auth" in api:  # APIs with no auth simply leave it out
            ref = api["auth"]["secretRef"]
            if not ref.startswith("secret://"):
                # Stops a pasted literal key from ever living in project.json
                raise SystemExit(f"{api['id']}: secretRef must start with secret://")
        for tool in api["tools"]:
            if tool["name"] in seen:
                raise SystemExit(f"Duplicate tool name: {tool['name']}")
            seen.add(tool["name"])
    return project


def load_secrets(project):
    keys = {}
    for api in project["apis"]:
        if "auth" not in api:
            continue
        ref = api["auth"]["secretRef"]
        account = ref.removeprefix("secret://")
        key = keyring.get_password(KEYCHAIN_SERVICE, account)
        if not key:
            # Name the ref, never the value
            raise SystemExit(f"Missing secret {ref} in the OS keychain")
        keys[api["id"]] = key
    return keys


parser = argparse.ArgumentParser(description="Serve the tools in a project.json over MCP stdio")
parser.add_argument("--project", required=True, help="Path to project.json")
PROJECT = load_project(parser.parse_args().project)
KEYS = load_secrets(PROJECT)  # fail at startup, not halfway through a chat
BYTE_CAP = PROJECT.get("responseByteCap", 50_000)

# tool name -> (api dict, tool dict)
TOOLS = {
    tool["name"]: (api, tool)
    for api in PROJECT["apis"]
    for tool in api["tools"]
    if tool.get("enabled", True)
}


# ------------------------------------------------------------ upstream HTTP ---

def location(loc, arg_name):
    """paramLocations entries are either "query" or {"in": "query", "name": "wireName"}.
    The object form lets an argument have a different name from the API's,
    e.g. a body field "id" exposed as "body_id" because a path param is also "id"."""
    if isinstance(loc, str):
        return loc, arg_name
    return loc["in"], loc.get("name", arg_name)


async def call_upstream(api, tool, arguments, key):
    base = api["baseUrl"].rstrip("/")
    path = tool["path"]
    query = {}
    headers = {"accept": "application/json"}
    body = None

    for arg_name, loc in tool.get("paramLocations", {}).items():
        if arg_name not in arguments:
            continue
        where, wire_name = location(loc, arg_name)
        value = arguments[arg_name]
        if where == "path":
            text = str(value)
            if text in (".", ".."):
                raise ValueError(f"Bad value for {arg_name}")
            path = path.replace("{" + wire_name + "}", quote(text, safe=""))
        elif where == "query":
            query[wire_name] = value  # httpx handles lists, numbers and booleans
        elif where == "header":
            headers[wire_name] = str(value)
        elif where == "body":
            body = body if isinstance(body, dict) else {}
            body[wire_name] = value
        elif where == "bodyRaw":
            body = value  # the whole JSON body, for APIs whose body isn't an object
        else:
            raise ValueError(f"Unknown parameter location {where!r} for {arg_name}")

    url = base + path

    # SSRF guard: a tool argument must never send the request to another host
    if urlsplit(url).netloc != urlsplit(base).netloc:
        raise ValueError("Blocked: request left the API host")

    # Auth goes in last, so a header argument can never replace it
    auth = api.get("auth")
    if auth:
        placement = auth["placement"]
        value = placement.get("prefix", "") + key  # e.g. "Bearer " for bearer tokens
        if placement["in"] == "header":
            headers[placement["name"]] = value
        else:
            query[placement["name"]] = value

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                tool["method"], url, params=query, headers=headers,
                json=body if body is not None else None,
            )
    except httpx.RequestError as e:
        # httpx errors can include the full URL, which holds the key when it's a
        # query parameter. Report the error type only.
        raise ValueError(f"Could not reach the API ({type(e).__name__})") from None

    if response.status_code >= 400:
        raise ValueError(f"Upstream returned {response.status_code}: {truncate(response.text, 2_000)}")
    return truncate(response.text, BYTE_CAP)


def truncate(text, cap):
    data = text.encode("utf-8")
    if len(data) <= cap:
        return text
    return data[:cap].decode("utf-8", errors="ignore") + f"\n[truncated {len(data) - cap} bytes]"


# -------------------------------------------------------------- MCP handlers ---

def tool_error(message):
    # is_error=True lets the model read the message and try again.
    # Raising instead would send a generic protocol error the model can't use.
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


async def list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
    tools = []
    for _, tool in TOOLS.values():
        # project.json stores tools in MCP's own wire format, so validate them as-is
        wire = {k: tool[k] for k in ("name", "description", "inputSchema", "annotations") if k in tool}
        tools.append(Tool.model_validate(wire))
    return ListToolsResult(tools=tools)


async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
    if params.name not in TOOLS:
        return tool_error(f"Unknown tool: {params.name}")
    api, tool = TOOLS[params.name]

    arguments = params.arguments or {}
    missing = [r for r in tool["inputSchema"].get("required", []) if r not in arguments]
    if missing:
        return tool_error(f"Missing required argument(s): {', '.join(missing)}")

    try:
        text = await call_upstream(api, tool, arguments, KEYS.get(api["id"]))
    except Exception as e:
        log(f"{params.name} failed: {e}")
        return tool_error(str(e))
    return CallToolResult(content=[TextContent(type="text", text=text)])


# -------------------------------------------------------------------- main ---

async def main():
    server = Server(PROJECT["name"], on_list_tools=list_tools, on_call_tool=call_tool)
    log(f"serving {len(TOOLS)} tool(s) from project '{PROJECT['name']}'")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    try:
        anyio.run(main)
    except KeyboardInterrupt:
        pass