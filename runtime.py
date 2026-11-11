"""Generic MCP runtime: serves the tools described in a project.json.

Local (Claude Desktop / Claude Code start it themselves, over stdio):
    uv run runtime.py --project C:\\full\\path\\to\\project.json

Remote (Streamable HTTP at /mcp, for claude.ai, mobile, and remote connectors):
    MCP_BEARER_TOKEN=... uv run runtime.py --project project.json --http --host 0.0.0.0 --port 8000

Secrets: for each secret://<account> ref the runtime reads the environment variable
MCP_SECRET_<ACCOUNT> first (servers have no OS keychain), then the OS keychain.

Nothing in here knows about any particular API. Add a tool to project.json,
restart the client, and it shows up. No code changes.
"""

import argparse
import hmac
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

import anyio
import httpx
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
SECRET_ENV_PREFIX = "MCP_SECRET_"
BEARER_ENV = "MCP_BEARER_TOKEN"
MIN_TOKEN_LENGTH = 32


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


def secret_env_name(ref):
    """secret://weather/openweather/apiKey -> MCP_SECRET_WEATHER_OPENWEATHER_APIKEY"""
    return SECRET_ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", ref.removeprefix("secret://")).upper()


def read_keychain(account):
    try:
        import keyring  # imported here: a server container may have no keychain backend at all
        return keyring.get_password(KEYCHAIN_SERVICE, account)
    except Exception:
        return None


def load_secrets(project):
    keys = {}
    for api in project["apis"]:
        if "auth" not in api:
            continue
        ref = api["auth"]["secretRef"]
        env_name = secret_env_name(ref)
        key = os.environ.get(env_name) or read_keychain(ref.removeprefix("secret://"))
        if not key:
            # Name the ref, never the value
            raise SystemExit(f"Missing secret {ref}: store it in the OS keychain, or set {env_name}")
        keys[api["id"]] = key
    return keys


def enabled_tools(project):
    """tool name -> (api dict, tool dict)"""
    return {
        tool["name"]: (api, tool)
        for api in project["apis"]
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


async def call_upstream(api, tool, arguments, key, byte_cap):
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
    return truncate(response.text, byte_cap)


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


def build_server(project, keys):
    tools = enabled_tools(project)
    byte_cap = project.get("responseByteCap", 50_000)

    async def list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        listed = []
        for _, tool in tools.values():
            # project.json stores tools in MCP's own wire format, so validate them as-is
            wire = {k: tool[k] for k in ("name", "description", "inputSchema", "annotations") if k in tool}
            listed.append(Tool.model_validate(wire))
        return ListToolsResult(tools=listed)

    async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        if params.name not in tools:
            return tool_error(f"Unknown tool: {params.name}")
        api, tool = tools[params.name]

        arguments = params.arguments or {}
        missing = [r for r in tool["inputSchema"].get("required", []) if r not in arguments]
        if missing:
            return tool_error(f"Missing required argument(s): {', '.join(missing)}")

        try:
            text = await call_upstream(api, tool, arguments, keys.get(api["id"]), byte_cap)
        except Exception as e:
            log(f"{params.name} failed: {e}")
            return tool_error(str(e))
        return CallToolResult(content=[TextContent(type="text", text=text)])

    server = Server(project["name"], on_list_tools=list_tools, on_call_tool=call_tool)
    return server, len(tools)


# ------------------------------------------------------------------- stdio ---

async def serve_stdio(server):
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# -------------------------------------------------------------------- http ---

class BearerGate:
    """ASGI wrapper: every request needs the token, except GET /health. Accepted as either
        Authorization: Bearer <token>    (Claude Code --header, most clients)
        X-API-Key: <token>               (claude.ai custom connectors, where Authorization
                                          may be reserved for OAuth)
    OAuth comes in milestone 6."""

    def __init__(self, app, token):
        self.app = app
        self.token = token.encode() if token else None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/health":
                return await self.respond(send, 200, b"ok", b"text/plain")
            if self.token is not None:
                headers = dict(scope.get("headers") or [])
                # Constant-time comparisons, both always evaluated
                bearer_ok = hmac.compare_digest(headers.get(b"authorization", b""), b"Bearer " + self.token)
                key_ok = hmac.compare_digest(headers.get(b"x-api-key", b""), self.token)
                if not (bearer_ok | key_ok):
                    return await self.respond(send, 401, b'{"error":"unauthorized"}', b"application/json",
                                              [(b"www-authenticate", b"Bearer")])
        await self.app(scope, receive, send)

    @staticmethod
    async def respond(send, status, body, content_type, extra=()):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", content_type), (b"content-length", str(len(body)).encode()),
                                *extra]})
        await send({"type": "http.response.body", "body": body})


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve_http(server, host, port, token):
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    if token:
        # The bearer token is what keeps strangers out. Host checks would also reject legitimate
        # public hostnames (your domain, a tunnel), so they're off when a token is required.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        security = None  # --no-auth is loopback-only, and the SDK then turns on DNS-rebinding protection

    # Stateless: every request stands alone (MCP spec 2026-07-28), so any number of copies
    # can run behind a load balancer and nothing breaks when one restarts.
    app = server.streamable_http_app(stateless_http=True, json_response=True, host=host,
                                     transport_security=security)
    uvicorn.run(BearerGate(app, token), host=host, port=port, log_level="warning")


# -------------------------------------------------------------------- main ---

def main():
    parser = argparse.ArgumentParser(description="Serve the tools in a project.json over MCP")
    parser.add_argument("--project", required=True, help="Path to project.json")
    parser.add_argument("--http", action="store_true", help="Serve Streamable HTTP at /mcp instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (0.0.0.0 inside a container)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")),
                        help="HTTP port (default: $PORT or 8000)")
    parser.add_argument("--no-auth", action="store_true",
                        help="HTTP without a token, for testing on 127.0.0.1 only")
    opts = parser.parse_args()

    project = load_project(opts.project)
    keys = load_secrets(project)  # fail at startup, not halfway through a chat
    server, count = build_server(project, keys)

    if not opts.http:
        log(f"serving {count} tool(s) from project '{project['name']}'")
        anyio.run(serve_stdio, server)
        return

    token = os.environ.get(BEARER_ENV, "").strip()
    if opts.no_auth:
        if not is_loopback(opts.host):
            raise SystemExit("--no-auth only works with --host 127.0.0.1. Anything else is reachable "
                             "by other machines, so it needs MCP_BEARER_TOKEN.")
        token = ""
    elif not token:
        raise SystemExit(f"Set {BEARER_ENV} to a long random token (uv run remote.py token --project ...). "
                         "Remote servers always require one, even on 127.0.0.1, because a tunnel "
                         "or proxy can make localhost public.")
    elif len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(f"{BEARER_ENV} is too short. Use at least {MIN_TOKEN_LENGTH} random characters.")

    log(f"serving {count} tool(s) from project '{project['name']}' at http://{opts.host}:{opts.port}/mcp"
        + ("" if token else " WITHOUT AUTH (local testing only)"))
    serve_http(server, opts.host, opts.port, token)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
