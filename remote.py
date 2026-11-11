"""Remote helpers: the inbound token, the settings a host needs, local serving and remote tests.

Usage (from the project folder):
    uv run remote.py token --project project.json [--new]
        Create (or show) the bearer token clients must send. Kept in the OS keychain.

    uv run remote.py env --project project.json [--values]
        List the environment variables the server needs on a host. --values prints the
        secret values too, for pasting into a host's secret settings.

    uv run remote.py serve --project project.json [--port 8000] [--public-url https://...]
        Run the HTTP server on 127.0.0.1 with the token and secrets from your keychain.
        Pair it with a tunnel (e.g. cloudflared) to try it from claude.ai before deploying.
        --public-url (default: the URL saved for the project) turns on OAuth sign-in.

    uv run remote.py test --project project.json --url https://host/mcp [--tool NAME --args JSON]
        Connect to a running remote server, list its tools, optionally call one.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from install import InstallError, read_json
from runtime import (BEARER_ENV, KEYCHAIN_SERVICE, MIN_TOKEN_LENGTH, TOKEN_DIR_ENV, check_public_url,
                     read_keychain, secret_env_name)

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime.py"


def inbound_account(project):
    return f"{project['name']}/_inbound/bearer"


def get_token(project):
    return read_keychain(inbound_account(project))


def create_token(project):
    import keyring
    token = secrets.token_urlsafe(32)  # 43 characters, 256 bits
    keyring.set_password(KEYCHAIN_SERVICE, inbound_account(project), token)
    return token


def public_base(project):
    """The saved server URL without /mcp, or None."""
    url = (project.get("deployment") or {}).get("url")
    return url[:-4] if url and url.endswith("/mcp") else url


def host_env(project, with_values=False):
    """[(name, value or None, what it is)] for every variable the server needs on a host."""
    rows = [(BEARER_ENV, get_token(project) if with_values else None,
             "The server's token: sign-in secret, signing key, and header token"),
            ("MCP_PUBLIC_URL", public_base(project) if with_values else None,
             "Public base URL, like https://my-server.example.com. Turns on OAuth sign-in")]
    oauth = False
    for api in project["apis"]:
        auth = api.get("auth")
        if not auth:
            continue
        ref = auth["secretRef"]
        value = read_keychain(ref.removeprefix("secret://")) if with_values else None
        if auth.get("oauth2"):
            oauth = True
            rows.append((secret_env_name(ref), value, f"Sign-in tokens for {api['id']} (JSON, from upstream_oauth.py login)"))
            client_ref = auth["oauth2"].get("clientSecretRef")
            if client_ref:
                client_value = read_keychain(client_ref.removeprefix("secret://")) if with_values else None
                rows.append((secret_env_name(client_ref), client_value, f"OAuth client secret for {api['id']}"))
        else:
            rows.append((secret_env_name(ref), value, f"Upstream secret for {api['id']}"))
    if oauth:
        rows.append((TOKEN_DIR_ENV, "/data/tokens" if with_values else None,
                     "Writable folder (a volume) so refreshed sign-in tokens survive restarts"))
    return rows


def is_local_url(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")


def validate_url(url):
    from urllib.parse import urlsplit
    url = (url or "").strip()
    parts = urlsplit(url)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise InstallError("The server URL must start with https:// (plain http only for 127.0.0.1). "
                           "A token sent over plain http can be read by anyone on the way.")
    if not parts.hostname:
        raise InstallError("That isn't a full URL, like https://my-server.example.com/mcp")
    if not parts.path.rstrip("/").endswith("/mcp"):
        raise InstallError("The URL should end in /mcp, which is where the runtime serves MCP.")
    return url.rstrip("/")


def docker_commands(project, project_file):
    image = f"mcp-{project['name']}"
    names = [name for name, _, _ in host_env(project)]
    flags = " ".join(f"-e {name}" for name in names if name != TOKEN_DIR_ENV)
    if TOKEN_DIR_ENV in names:  # a volume, so refreshed OAuth tokens survive restarts
        flags += f" -v {image}-tokens:/data/tokens -e {TOKEN_DIR_ENV}=/data/tokens"
    return [
        f"docker build --build-arg PROJECT={project_file} -t {image} .",
        f"docker run --rm -p 8000:8000 {flags} {image}",
    ]


def remote_session(url, token, tool=None, arguments=None):
    """Connect over Streamable HTTP. Returns (tools, call result or None)."""
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def run():
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with anyio.fail_after(60):
            async with create_mcp_http_client(headers=headers) as http:
                async with streamable_http_client(url, http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        tools = (await session.list_tools()).tools
                        result = await session.call_tool(tool, arguments or {}) if tool else None
                        return tools, result

    # The MCP client reports HTTP failures as a generic error, so probe first for a clear message
    import httpx
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        probe = httpx.post(url, headers={**headers, "accept": "application/json, text/event-stream"},
                           json={}, timeout=20)
    except httpx.RequestError as err:
        raise InstallError(f"Couldn't reach {url} ({type(err).__name__}). Check the URL and that the "
                           "server is running.") from None
    if probe.status_code == 401:
        raise InstallError("The server rejected the token (401). The host's MCP_BEARER_TOKEN must match "
                           "the token in your keychain (remote.py token).")
    if probe.status_code == 404:
        raise InstallError(f"{url} returned 404. The MCP endpoint is at /mcp.")
    if probe.status_code >= 500:
        # e.g. Cloudflare's 502/530 when the tunnel is up but nothing answers behind it
        raise InstallError(f"{url} answered {probe.status_code}: the tunnel or host is up but can't reach "
                           "your server. Check that remote.py serve (or your container) is running, and "
                           "that the tunnel URL is the current one: it changes every time cloudflared restarts.")

    try:
        return anyio.run(run)
    except Exception as err:
        raise InstallError(f"Reached {url}, but the MCP session failed ({type(err).__name__}). "
                           "Check the server's logs.") from None


# ----------------------------------------------------------------- commands ---

def load(opts):
    path = Path(opts.project).resolve()
    return path, read_json(path)


def cmd_token(opts):
    _, project = load(opts)
    token = get_token(project)
    if token and not opts.new:
        print(token)
        print("(existing token; --new replaces it, and every client then needs the new one)", file=sys.stderr)
        return
    token = create_token(project)
    print(token)
    print("Stored in the OS keychain. Set it as MCP_BEARER_TOKEN on the host, and send it as\n"
          f"'Authorization: Bearer <token>' from clients.", file=sys.stderr)


def cmd_env(opts):
    path, project = load(opts)
    if opts.values:
        print("These are secrets. Paste them into your host's secret settings, then clear this terminal.\n",
              file=sys.stderr)
    for name, value, what in host_env(project, opts.values):
        if opts.values:
            print(f"{name}={value if value else '<missing: store it first>'}")
        else:
            print(f"{name:<45} {what}")
    print("\nDocker:", file=sys.stderr)
    for line in docker_commands(project, path.relative_to(HERE).as_posix() if path.is_relative_to(HERE) else path.name):
        print(f"  {line}", file=sys.stderr)


def cmd_serve(opts):
    path, project = load(opts)
    token = get_token(project)
    if not token:
        raise InstallError("No token yet. Run: uv run remote.py token --project " + opts.project)
    env = dict(os.environ)
    env[BEARER_ENV] = token
    public = opts.public_url or public_base(project)
    if public:
        public = check_public_url(public)  # fail here with a clear message, not a traceback
    args = [sys.executable, str(RUNTIME), "--project", str(path), "--http", "--host", "127.0.0.1",
            "--port", str(opts.port)]
    if public:
        args += ["--public-url", public]
    print(f"Serving on http://127.0.0.1:{opts.port}/mcp with your token. Ctrl+C to stop.", file=sys.stderr)
    if public:
        print(f"OAuth sign-in is on for {public}. If that's an old tunnel URL, pass --public-url "
              "with the current one or save it in the GUI.", file=sys.stderr)
    try:
        subprocess.run(args, env=env)
    except KeyboardInterrupt:
        pass


def cmd_test(opts):
    _, project = load(opts)
    token = opts.token or get_token(project)
    if not token:
        raise InstallError("No token. Pass --token or run remote.py token first.")
    arguments = json.loads(opts.args) if opts.args else {}
    tools, result = remote_session(opts.url, token, opts.tool, arguments)
    print(f"Connected. {len(tools)} tool(s):")
    for t in tools:
        print(f"  - {t.name}")
    if result is not None:
        print(f"\n{opts.tool}: {'error' if result.is_error else 'ok'}")
        print("\n".join(getattr(c, "text", "") for c in result.content))


def main():
    parser = argparse.ArgumentParser(description="Remote MCP server helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("token"); p.add_argument("--project", required=True); p.add_argument("--new", action="store_true")
    p = sub.add_parser("env"); p.add_argument("--project", required=True); p.add_argument("--values", action="store_true")
    p = sub.add_parser("serve"); p.add_argument("--project", required=True); p.add_argument("--port", type=int, default=8000)
    p.add_argument("--public-url", help="Public base URL; turns on OAuth sign-in (default: the saved URL)")
    p = sub.add_parser("test")
    p.add_argument("--project", required=True)
    p.add_argument("--url", required=True, help="Full URL ending in /mcp")
    p.add_argument("--token", help="Defaults to the token in your keychain")
    p.add_argument("--tool")
    p.add_argument("--args", help='JSON object, e.g. \'{"q": "Boise,US"}\'')

    opts = parser.parse_args()
    try:
        {"token": cmd_token, "env": cmd_env, "serve": cmd_serve, "test": cmd_test}[opts.command](opts)
    except InstallError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
