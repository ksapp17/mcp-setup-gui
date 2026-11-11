"""Generic MCP runtime: serves the tools described in a project.json.

Local (Claude Desktop / Claude Code start it themselves, over stdio):
    uv run runtime.py --project C:\\full\\path\\to\\project.json

Remote (Streamable HTTP at /mcp, for claude.ai, mobile, and remote connectors):
    MCP_BEARER_TOKEN=... uv run runtime.py --project project.json --http --host 0.0.0.0 --port 8000
    Add --public-url https://your-host (or MCP_PUBLIC_URL) to turn on OAuth sign-in (oauth.py).

Secrets: for each secret://<account> ref the runtime reads the environment variable
MCP_SECRET_<ACCOUNT> first (servers have no OS keychain), then the OS keychain.
APIs that use OAuth (upstream_oauth.py) store a token bundle there instead of a key;
the runtime refreshes it and saves rotated tokens back. On a host, set MCP_TOKEN_DIR
to a writable folder so refreshed tokens survive restarts.

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
from upstream_oauth import OAuthError, TokenSource, check_config, dump_bundle, oauth_config
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
TOKEN_DIR_ENV = "MCP_TOKEN_DIR"
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


def write_keychain(account, value):
    import keyring
    keyring.set_password(KEYCHAIN_SERVICE, account, value)


def token_file(ref):
    folder = os.environ.get(TOKEN_DIR_ENV)
    return Path(folder) / (secret_env_name(ref) + ".json") if folder else None


def read_secret(ref):
    """(value, where) for a secret:// ref. A refreshed token in MCP_TOKEN_DIR wins, since
    it's newer than whatever was pasted into the environment."""
    file = token_file(ref)
    value = os.environ.get(secret_env_name(ref))
    if file and file.is_file():
        saved = file.read_text(encoding="utf-8").strip()
        # After a fresh sign-in, the new bundle pasted into the environment is newer than the file
        if not value or issued_at(saved) >= issued_at(value):
            return saved, "file"
    if value:
        return value, "env"
    value = read_keychain(ref.removeprefix("secret://"))
    return (value, "keychain") if value else (None, None)


def issued_at(raw):
    try:
        return int(json.loads(raw).get("issued_at", 0))
    except (ValueError, AttributeError, TypeError):
        return 0


def token_saver(api_id, ref, where):
    """Where a refreshed token bundle goes: the token dir if set, else back to the keychain.
    An environment variable can't be updated, so with neither, the new tokens live in memory."""
    def save(bundle):
        file = token_file(ref)
        if file:
            file.parent.mkdir(parents=True, exist_ok=True)
            tmp = file.with_suffix(".tmp")
            tmp.write_text(dump_bundle(bundle), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, file)
        elif where == "keychain":
            write_keychain(ref.removeprefix("secret://"), dump_bundle(bundle))
        else:
            log(f"{api_id}: refreshed token kept in memory only. Set {TOKEN_DIR_ENV} to a writable "
                "folder so it survives a restart.")
    return save


def load_secrets(project, project_path="<project.json>"):
    keys = {}
    for api in project["apis"]:
        if "auth" not in api:
            continue
        ref = api["auth"]["secretRef"]
        value, where = read_secret(ref)
        cfg = oauth_config(api)
        if not cfg:
            if not value:
                # Name the ref, never the value
                raise SystemExit(f"Missing secret {ref}: store it in the OS keychain, or set {secret_env_name(ref)}")
            keys[api["id"]] = value
            continue

        try:
            check_config(api)
        except OAuthError as e:
            raise SystemExit(str(e))
        if not value:
            raise SystemExit(f"{api['id']} isn't signed in. Run: uv run upstream_oauth.py login "
                             f"--project {project_path} --api {api['id']}  (on a host: set {secret_env_name(ref)})")
        client_secret = None
        if cfg.get("clientSecretRef"):
            client_secret, _ = read_secret(cfg["clientSecretRef"])
            if not client_secret:
                raise SystemExit(f"Missing secret {cfg['clientSecretRef']} (the OAuth app's client secret): "
                                 f"store it with upstream_oauth.py client-secret, or set "
                                 f"{secret_env_name(cfg['clientSecretRef'])}")
        keys[api["id"]] = TokenSource(api["id"], cfg, value, client_secret,
                                      load_latest=lambda ref=ref: read_secret(ref)[0],
                                      save=token_saver(api["id"], ref, where), log=log,
                                      project_path=project_path)
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

    auth = api.get("auth")
    oauth = isinstance(key, TokenSource)

    async def send(secret):
        # Auth goes in last, so a header argument can never replace it
        h, q = dict(headers), dict(query)
        if auth:
            placement = auth["placement"]
            value = placement.get("prefix", "") + secret  # e.g. "Bearer " for bearer tokens
            if placement["in"] == "header":
                h = {k: v for k, v in h.items() if k.lower() != placement["name"].lower()}
                h[placement["name"]] = value
            else:
                q[placement["name"]] = value
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                return await client.request(
                    tool["method"], url, params=q, headers=h,
                    json=body if body is not None else None,
                )
        except httpx.RequestError as e:
            # httpx errors can include the full URL, which holds the key when it's a
            # query parameter. Report the error type only.
            raise ValueError(f"Could not reach the API ({type(e).__name__})") from None

    secret = await key.access_token() if oauth else key
    response = await send(secret)
    if oauth and response.status_code == 401 and key.can_refresh:
        # Revoked or expired early: refresh once and retry
        await key.refresh(secret)
        response = await send(key.bundle["access_token"])

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

    def __init__(self, app, token, oauth=False):
        self.app = app
        self.token = token.encode() if token else None
        self.oauth = oauth  # with OAuth on, the SDK checks tokens and answers 401 with sign-in details

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/health":
                return await self.respond(send, 200, b"ok", b"text/plain")
            if self.oauth:
                if scope["path"] == "/mcp":
                    scope = self.translate_api_key(scope)
                return await self.app(scope, receive, send)
            if self.token is not None:
                headers = dict(scope.get("headers") or [])
                # Constant-time comparisons, both always evaluated
                bearer_ok = hmac.compare_digest(headers.get(b"authorization", b""), b"Bearer " + self.token)
                key_ok = hmac.compare_digest(headers.get(b"x-api-key", b""), self.token)
                if not (bearer_ok | key_ok):
                    return await self.respond(send, 401, b'{"error":"unauthorized"}', b"application/json",
                                              [(b"www-authenticate", b"Bearer")])
        await self.app(scope, receive, send)

    def translate_api_key(self, scope):
        """X-API-Key: <token> becomes Authorization: Bearer <token>, which the SDK's checks accept."""
        headers = list(scope.get("headers") or [])
        given = dict(headers).get(b"x-api-key")
        if given and hmac.compare_digest(given, self.token):
            headers = [(k, v) for k, v in headers if k != b"authorization"]
            headers.append((b"authorization", b"Bearer " + self.token))
            scope = {**scope, "headers": headers}
        return scope

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


def serve_http(server, host, port, token, public_url=None, server_name="mcp"):
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
    options = dict(stateless_http=True, json_response=True, host=host, transport_security=security)

    if public_url:
        # OAuth sign-in (claude.ai "Sign in now"). The SDK serves the OAuth endpoints and
        # metadata; oauth.py supplies stateless storage and the /login page.
        import oauth
        from mcp.server.auth.provider import ProviderTokenVerifier
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

        resource = public_url + "/mcp"
        provider = oauth.OwnerOAuthProvider(token, public_url, resource, server_name)
        options.update(
            auth=AuthSettings(
                issuer_url=public_url,
                resource_server_url=resource,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=[oauth.SCOPE], default_scopes=[oauth.SCOPE]),
                validate_token_resource=True,  # tokens only work for this server (RFC 8707)
            ),
            token_verifier=ProviderTokenVerifier(provider),
            auth_server_provider=provider,
            custom_starlette_routes=provider.login_routes(),
        )

    app = server.streamable_http_app(**options)
    uvicorn.run(BearerGate(app, token, oauth=bool(public_url)), host=host, port=port, log_level="warning")


def check_public_url(url):
    url = url.strip().rstrip("/")
    if url.endswith("/mcp"):
        url = url[:-4]
    parts = urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback(parts.hostname or "")):
        raise SystemExit("--public-url must be https:// (http only for 127.0.0.1 or localhost).")
    if parts.path not in ("", "/"):
        raise SystemExit("--public-url is the server's base URL, like https://my-server.example.com")
    try:
        parts.port  # raises on junk like "127.0.0.1:8000.trycloudflare.com"
        valid = True
    except ValueError:
        valid = False
    host = parts.hostname or ""
    if not host or not valid:
        raise SystemExit(f"--public-url {url!r} isn't a valid URL. Copy the https://....trycloudflare.com "
                         "address cloudflared printed, or your host's URL.")
    if parts.scheme == "https" and is_loopback(host):
        raise SystemExit("--public-url is the public address (your tunnel or host), not 127.0.0.1. "
                         "Use the https://....trycloudflare.com URL cloudflared printed.")
    return url


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
    parser.add_argument("--public-url", default=os.environ.get("MCP_PUBLIC_URL", ""),
                        help="Public base URL (https://...). Turns on OAuth sign-in. Default: $MCP_PUBLIC_URL")
    opts = parser.parse_args()

    project = load_project(opts.project)
    keys = load_secrets(project, opts.project)  # fail at startup, not halfway through a chat
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

    public_url = check_public_url(opts.public_url) if opts.public_url else None
    if public_url and not token:
        raise SystemExit("OAuth sign-in needs MCP_BEARER_TOKEN: it's the owner's sign-in secret and signing key.")

    log(f"serving {count} tool(s) from project '{project['name']}' at http://{opts.host}:{opts.port}/mcp"
        + ("" if token else " WITHOUT AUTH (local testing only)")
        + (f", OAuth sign-in on for {public_url}" if public_url else ""))
    serve_http(server, opts.host, opts.port, token, public_url, project["name"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
