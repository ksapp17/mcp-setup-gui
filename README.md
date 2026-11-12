# mcp-setup-gui

[Project page](https://ksapp17.github.io/mcp-setup-gui/) · ![tests](https://github.com/ksapp17/mcp-setup-gui/actions/workflows/tests.yml/badge.svg)

Turn an HTTP API into tools Claude can use, without writing server code or hand-editing config files.

You describe an API in `project.json`. A generic MCP runtime serves every endpoint you list as a tool, and an installer registers it with Claude Desktop safely: it verifies the server first, shows a diff, backs up the config, and can undo the change.

**Status:** milestones 1 to 6 of 7 work, plus installing servers from the MCP Registry, with a browser UI: local servers for Claude Desktop and Claude Code, remote servers for claude.ai and mobile with OAuth sign-in, API-key, bearer and OAuth auth to upstream APIs, import from OpenAPI specs, and a built-in tool tester. More clients are planned (see [Roadmap](#roadmap)).

```
project.json ──► runtime.py ──stdio──► Claude Desktop ──► Claude
     ▲               │
     │               └──► upstream API (key from the OS keychain)
 install.py ──► claude_desktop_config.json (backup, diff, atomic write)
```

## Why

Connecting Claude to an API today means writing an MCP server, then editing a JSON config file by hand. That config file is shared with the app's own settings, sits in different places on different installs, and one missing comma silently disables every server. API keys usually end up pasted into that file in plain text.

This project makes the server a data file and the install a checked, reversible operation.

## Browser UI

```powershell
uv run gui.py
```

It opens a page in your browser where you can:

- **Import an API** from an OpenAPI spec URL or file, tick which operations become tools, and edit their descriptions
- **Store each API's secret** in the OS keychain
- **Turn tools on and off** and edit descriptions later
- **Test a tool**: run it the way Claude Desktop does and see the response, the raw MCP result and the runtime's log
- **Install or remove** the project in Claude Desktop, after a preview of the exact change to the config file
- **Register it with Claude Code** in user, local or project scope, after a preview of the exact command
- **Run it as a remote server** for claude.ai and the mobile apps: create the token, see what the host needs, test the deployed server, and register it

The UI is plain HTML forms served by Python. It listens on `127.0.0.1` only, and the terminal prints a one-time link that signs your browser in. It uses the same code as the command-line tools below, so you can mix the two.

## Quick start (Windows, PowerShell)

Requirements: [uv](https://docs.astral.sh/uv/) and Claude Desktop.

```powershell
git clone https://github.com/ksapp17/mcp-setup-gui.git
cd mcp-setup-gui
uv sync
```

**1. Store your API key in the OS keychain.** The input is hidden and never touches a file:

```powershell
uv run python -c "import keyring, getpass; keyring.set_password('mcp-setup-gui', 'weather/openweather/apiKey', getpass.getpass('API key: '))"
```

The included `project.json` uses [OpenWeather](https://openweathermap.org/api) (free key).

**2. Quit Claude Desktop.** It saves its own settings to the same config file and can overwrite the change.

```powershell
Get-Process claude -ErrorAction SilentlyContinue | Stop-Process
```

**3. Install:**

```powershell
uv run install.py add --project project.json
```

**4. Reopen Claude Desktop** and ask: *"What's the weather in Boise this weekend?"*

## Claude Code

```powershell
uv run install.py add --project project.json --client claude-code                       # every project (user scope)
uv run install.py add --project project.json --client claude-code --scope local --folder C:\code\my-repo
uv run install.py remove --project project.json --client claude-code
```

This goes through Claude Code's own `claude mcp add` and `claude mcp remove` instead of editing its files, because `~/.claude.json` holds all of Claude Code's state and is rewritten while it runs. The installer verifies the runtime first, shows the exact command, replaces its own earlier registration in the same scope, and refuses to overwrite a server it didn't create.

| Scope | Where it applies | Stored in |
| --- | --- | --- |
| `user` (default) | Every project on this machine | `~/.claude.json` |
| `local` | One folder, only you | `~/.claude.json`, keyed by the folder |
| `project` | One folder, shared through git | `.mcp.json` in that folder |

Project scope writes this machine's absolute paths into a file meant to be committed, so teammates would need the same paths. Prefer `user` or `local`. Start a new Claude Code session and run `/mcp` to check the server.

## Remote server (claude.ai, mobile, connectors)

claude.ai and the Claude mobile apps connect to MCP servers from Anthropic's cloud, so the server has to run somewhere public over HTTPS. The same `runtime.py` serves Streamable HTTP at `/mcp`, stateless, so any number of copies can run behind a load balancer.

```powershell
uv run remote.py token --project project.json          # create the bearer token (kept in the OS keychain)
uv run remote.py env --project project.json            # the environment variables a host needs
uv run remote.py serve --project project.json          # run it on 127.0.0.1:8000 with your token
uv run remote.py test --project project.json --url https://your-host/mcp --tool get_current_weather --args '{"q": "Boise,US"}'
```

**Try it from your PC first** with a free Cloudflare quick tunnel (install `cloudflared`, no account needed): start `cloudflared tunnel --url http://127.0.0.1:8000` in one terminal. It prints a temporary `https://...trycloudflare.com` URL. Then run `uv run remote.py serve --project project.json --public-url https://....trycloudflare.com` in another (or save the URL plus `/mcp` in the GUI and leave `--public-url` out). The connector URL is the tunnel URL plus `/mcp`.

**Deploy** to any host that runs containers (Render, Railway, Fly.io, a VPS) with the included `Dockerfile`:

```powershell
docker build --build-arg PROJECT=project.json -t mcp-weather .
docker run --rm -p 8000:8000 -e MCP_BEARER_TOKEN -e MCP_SECRET_WEATHER_OPENWEATHER_APIKEY mcp-weather
```

Set `MCP_PUBLIC_URL` to the host's public base URL to turn on sign-in. No secret is baked into the image. On a host there's no OS keychain, so the runtime reads each `secret://<account>` from an environment variable named `MCP_SECRET_<ACCOUNT>` (`remote.py env` lists them; `--values` prints the values for pasting into the host's secret settings).

**Connect Claude:**

- **claude.ai, mobile and Claude Desktop, with sign-in** (works in every account): run the server with a public URL, which turns on OAuth. In claude.ai, Settings → Connectors → Add custom connector, enter the URL, keep **Sign in now**, and pick **dynamic client registration** for the OAuth client. When you click Connect, your server's own sign-in page opens: paste the token and click Allow.
- **Or with a header** (accounts with the Request headers beta): choose **No sign-in**, then add the request header `x-api-key` with the token as its value.
- **Claude Code:** `uv run install.py add --project project.json --client claude-code --url https://your-host/mcp` registers `<name>-remote` with the token as a header.

### How sign-in works

The runtime includes a small OAuth 2.1 authorization server for one owner (`oauth.py`): claude.ai discovers it from the 401 response, registers itself, and sends you to the server's `/login` page, where you approve it by entering the server's token. PKCE, redirect URIs and client credentials are checked by the MCP SDK.

It stores nothing. Registered clients, codes and tokens are signed with a key derived from `MCP_BEARER_TOKEN`, so they survive restarts and redeploys, and replacing the token (`remote.py token --new`) signs every client out at once. Access tokens last an hour and refresh tokens 30 days. Tokens are bound to this server's URL (RFC 8707), and redirect URIs must be `https` or loopback.

## APIs that use OAuth (GitHub, Google, Spotify, ...)

Some APIs don't hand out keys: you sign in, and the tool acts as you. Give the API an `auth.oauth2` block (see [project.json](#projectjson); the importer writes one from the spec), then:

1. **Register an OAuth app** on the provider's developer site. Use redirect URL `http://127.0.0.1/callback`. Copy the client ID into `auth.oauth2.clientId`.
2. **Store the client secret:** `uv run upstream_oauth.py client-secret --project project.json` (input is hidden).
3. **Sign in:** `uv run upstream_oauth.py login --project project.json`. The provider's page opens; approve it and the tokens go into the keychain.
4. **Restart Claude.** The runtime sends the access token and refreshes it before it expires.

The browser UI does the same from the API's card: client ID and secret fields, a **Sign in** button, and the current status. `upstream_oauth.py status` shows when tokens expire; `logout` deletes them.

**Example: GitHub.** Settings → Developer settings → OAuth Apps → New OAuth App. Homepage `http://127.0.0.1`, callback `http://127.0.0.1/callback`. Generate a client secret. New GitHub OAuth apps issue tokens that expire after 8 hours with a refresh token, which the runtime handles. GitHub also ignores the port on loopback redirects, so the sign-in can use any free port.

```json
"auth": {
  "secretRef": "secret://github/github/oauth",
  "placement": { "in": "header", "name": "Authorization", "prefix": "Bearer " },
  "oauth2": {
    "authorizeUrl": "https://github.com/login/oauth/authorize",
    "tokenUrl": "https://github.com/login/oauth/access_token",
    "clientId": "Ov23li...",
    "clientSecretRef": "secret://github/github/clientSecret",
    "scopes": ["read:user"]
  }
}
```

**Providers that need an exact redirect URL** (Spotify, for one) need a fixed port: set `"redirectPort": 8765` and register `http://127.0.0.1:8765/callback`. **Google** only returns a refresh token if asked: add `"extraAuthorizeParams": {"access_type": "offline", "prompt": "consent"}`.

**On a remote server**, sign in on your PC first, then copy the values `remote.py env --values` prints (the token bundle is one JSON value) to the host. Providers like GitHub replace the refresh token every time it's used, so set `MCP_TOKEN_DIR` to a folder on a persistent volume: the runtime saves refreshed tokens there, and they survive restarts. Without it, a restart after the first refresh means signing in again.

## Install servers from the MCP Registry

The [MCP Registry](https://registry.modelcontextprotocol.io) is the official catalog of ready-made MCP servers. In the browser UI, click **install a ready-made server** on the home page, search by name, pick an install option, fill in what the server asks for, preview, and install. Or from the terminal:

```powershell
uv run registry.py search github
uv run registry.py show io.github.someone/server      # install options and what each needs
uv run registry.py install io.github.someone/server   # asks for each setting; secrets are hidden
uv run registry.py list
uv run registry.py remove server
```

| Server type | How it runs | Needs |
| --- | --- | --- |
| npm package | `npx -y package@version` | Node.js |
| PyPI package | `uvx package==version` | uv |
| Docker image | `docker run -i --rm image` | Docker |
| NuGet package | `dnx package@version --yes` | .NET 10 SDK |
| Remote URL | Claude Code by URL; Desktop and claude.ai as a custom connector | nothing |

Packages run through `launch.py`, which reads their API keys from the OS keychain when the client starts them, so the keys stay out of Claude's config files (the usual way puts them there in plain text). Settings that aren't secret are kept in `~/.mcpgui/servers/<name>.json`. Install options are `--client claude-code`, `--option ID` (from `show`) and `--name` (the name in Claude). MCPB bundles and packages that run their own HTTP server aren't supported yet.

The registry checks that a publisher owns the name, not what the code does. Read the source before installing a server, especially one that asks for keys.

## Import any API from its OpenAPI spec

```powershell
uv run import_openapi.py https://example.com/openapi.json --name myapi --out myapi.json
```

The importer resolves the spec's `$ref`s, works out the base URL and auth scheme, and turns each operation into a candidate tool. You pick which ones become tools in a terminal list: read-only operations (GET, HEAD) are pre-selected, writes are marked `[writes]` and DELETE operations `[destructive]`. It then writes `myapi.json` and prints the command to store the secret and install.

| Option | Use |
| --- | --- |
| `--out PATH` | Project file to create, or add this API to if it exists |
| `--name NAME` | Project name for a new project (the server name Claude shows) |
| `--api-id ID` | Id for this API inside the project |
| `--base-url URL` | Needed when the spec's server URL is relative |
| `--filter TEXT` | Only list operations whose path, name or tag contains TEXT. Useful for big specs. |
| `--select WHICH` | Skip the picker: `readonly`, `all`, or comma-separated tool names |
| `--force` | Replace an API with the same id already in the project |

Supported: OpenAPI 3.0 and 3.1, JSON or YAML, path/query/header parameters, JSON request bodies, API-key (header or query), bearer, and OAuth authorization-code auth (you fill in the client ID afterwards). Not yet: Swagger 2.0 (convert it first), external `$ref`s, form and multipart bodies (those operations are skipped and listed), cookie auth, basic auth, OpenID Connect discovery and other OAuth flows (configured as a bearer token you paste).

After importing, read the tool descriptions in the project file. The importer lists any that are short, and the description is what Claude uses to decide when to call a tool.

## Commands

| Command | What it does |
| --- | --- |
| `uv run install.py add --project project.json` | Verify and register the project with Claude Desktop |
| `uv run install.py remove --project project.json` | Remove the entry this installer added |
| `uv run install.py add --project project.json --client claude-code` | Register with Claude Code (see above) |
| `uv run install.py add --project project.json --client claude-code --url URL` | Register a remote server with Claude Code |
| `uv run remote.py token / env / serve / test` | Remote server helpers (see above); `serve --public-url URL` turns on sign-in |
| `uv run upstream_oauth.py client-secret / login / status / logout --project project.json` | Sign in to APIs that use OAuth (see above). Add `--api ID` when there's more than one |
| `uv run registry.py search / show / install / list / remove` | Install ready-made servers from the MCP Registry (see above) |
| `uv run install.py list` | Show every MCP server in each Claude Desktop config found |
| `uv run gui.py` | Open the browser UI (`--no-browser` to just print the link) |
| `uv run import_openapi.py SPEC --out project.json` | Turn an OpenAPI spec into tools (see above) |
| `uv run runtime.py --project project.json` | Run the server by hand (it waits for a client on stdin; Ctrl+C to stop) |

Options for `add` and `remove`: `--config PATH` to pick the config file, `--yes` to skip prompts. `add` also takes `--skip-verify`.

## project.json

One project becomes one MCP server. Each API has a base URL, how to authenticate, and the tools it exposes:

```json
{
  "name": "weather",
  "responseByteCap": 50000,
  "apis": [
    {
      "id": "openweather",
      "baseUrl": "https://api.openweathermap.org/data/2.5",
      "auth": {
        "secretRef": "secret://weather/openweather/apiKey",
        "placement": { "in": "query", "name": "appid" }
      },
      "tools": [
        {
          "name": "get_current_weather",
          "description": "Current weather for a city. Use when the user asks what the weather is like right now.",
          "method": "GET",
          "path": "/weather",
          "paramLocations": { "q": "query", "units": "query" },
          "inputSchema": {
            "type": "object",
            "properties": {
              "q": { "type": "string", "description": "City, e.g. Boise,US" },
              "units": { "type": "string", "enum": ["imperial", "metric"] }
            },
            "required": ["q", "units"]
          },
          "annotations": { "readOnlyHint": true }
        }
      ]
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `auth` | Optional. Leave it out for public APIs. |
| `auth.secretRef` | Keychain reference, `secret://<account>`. Stored under service `mcp-setup-gui`. Literal keys are rejected. |
| `auth.placement` | Where the key goes: `{"in": "query" \| "header", "name": "...", "prefix": "Bearer "}`. `prefix` is optional. |
| `auth.oauth2` | Optional. Makes the secret a sign-in instead of a key: `authorizeUrl`, `tokenUrl`, `clientId`, `clientSecretRef` (if the provider issued a secret), `scopes`. Optional: `redirectPort`, `redirectPath` (default `/callback`), `tokenAuth` (`post` or `basic`), `extraAuthorizeParams`. |
| `paramLocations` | For each argument: `"path"` (fills `{name}` in `path`), `"query"`, `"header"`, `"body"` (a field of the JSON body) or `"bodyRaw"` (the whole body). The object form `{"in": "body", "name": "id"}` lets an argument's name differ from the API's. |
| `inputSchema` | JSON Schema for the tool's arguments, passed to Claude as-is |
| `description` | What Claude reads to decide when to call the tool. The main quality lever. |
| `annotations` | MCP tool hints, e.g. `readOnlyHint` |
| `enabled` | Optional, defaults to `true` |
| `responseFields` | Optional. Keep only these parts of a JSON response, as dot paths: `["name", "types.type.name", "stats.base_stat"]`. Lists are walked through, so `stats.base_stat` keeps `base_stat` in every item of `stats`. The tester lists every path in a response to pick from. |
| `responseMaxItems` | Optional. Shorten every list in the response to this many items, noting how many were left out. |
| `responseByteCap` | Responses longer than this are truncated (default 50,000 bytes) |

Adding a tool is a JSON edit and a Claude Desktop restart. No code changes.

**Keep responses small.** Everything a tool returns goes into Claude's context. Some APIs send a lot: one PokéAPI Pokémon is over 300 KB, mostly move lists, and the useful fields come after them, so the cap cuts them off. Run the tool in the tester: it shows how many bytes Claude would get, warns when the cap cuts the response off, and lists the response's fields. Put the ones you need in **Keep only these response fields** for that tool.

## Security design

- **Secrets live in the OS keychain only.** `project.json`, the Claude Desktop config, the repo and the logs hold a `secret://` reference, never the value. The runtime reads the keychain itself at startup.
- **Errors never leak the key.** Network errors are reported by type only, because the request URL contains the key when it's a query parameter.
- **Requests stay on the configured host.** Path arguments are URL-encoded and the final URL's host is checked against `baseUrl`, so a tool argument can't redirect a request elsewhere. Auth is applied after header arguments, so an argument can't replace it.
- **Upstream sign-in follows RFC 8252.** The authorization code flow with PKCE (S256), a random `state` checked in constant time, and a redirect to a listener on `127.0.0.1` that exists only during the sign-in and doesn't log request lines (they hold the code). Tokens and the client secret live in the keychain; status pages show expiry, never a token. Refreshed tokens are saved back, and two Claude clients sharing one sign-in pick up each other's refreshes instead of using a spent refresh token.
- **Writes are opt-in.** The importer pre-selects only read-only operations and marks DELETE operations with `destructiveHint`, so clients can ask before running them.
- **Config writes are checked and reversible.** The installer refuses to touch a config it can't parse, shows a diff, backs up to `~/.mcpgui/backups`, writes to a temp file and swaps it in, re-reads the result, and restores the backup if it doesn't match.
- **Nothing is installed blind.** Before writing, the installer starts the runtime exactly as Claude Desktop will and lists the tools Claude is about to get.
- **The UI only answers to you.** It binds to `127.0.0.1`, signs in with a random one-time token, rejects requests with an unexpected `Host` header (DNS rebinding) or a foreign `Origin` (cross-site form posts), and sends a Content-Security-Policy that allows no scripts. A config change previewed in the UI is refused if the file changed before you confirm.
- **Remote servers always require a bearer token**, even on 127.0.0.1, because a tunnel or proxy can make localhost public. The token is accepted as `Authorization: Bearer <token>` or `X-API-Key: <token>`. Tokens are 256-bit random values, compared in constant time, and refused if shorter than 32 characters. URLs must be `https://` (plain `http` only for 127.0.0.1). The GUI and CLI never print a token in a command preview, and GUI pages are sent with `Cache-Control: no-store`.
- **Merges, never overwrites.** Other servers and the app's own settings are left untouched. Replacing an entry the installer didn't create needs explicit confirmation.

## Troubleshooting

**Claude Desktop shows "No servers added."** It's reading a different config file, or it hasn't restarted. Check *Settings → Desktop app → Extensions → Edit config* to see which file it uses. On some Windows installs that's `%LOCALAPPDATA%\Claude-Data\claude_<id>\claude_desktop_config.json`, not the `%APPDATA%\Claude` path most guides give. `install.py` searches both.

**The server shows as failed.** Read its log. The runtime writes errors to stderr, which Claude Desktop records per server (`mcp-server-<name>.log`).

**The UI says Claude Code isn't available.** The `claude` command has to be on PATH for the terminal that runs `gui.py`. Open a new terminal after installing Claude Code.

**A remote test says 401.** The host's `MCP_BEARER_TOKEN` doesn't match the token in your keychain. Run `remote.py token` to see yours, or `--new` to replace it (then update the host and every client).

**claude.ai says it couldn't register with the sign-in service.** The server isn't running with a public URL, so OAuth is off. Restart `remote.py serve` with `--public-url` set to the current tunnel or host URL.

**The sign-in page link is broken.** The public URL changed (a new tunnel) but the server still uses the old one. Restart it with the new URL.

**A tool says the sign-in has expired.** The refresh token was revoked or ran out (GitHub's last 6 months unused). Run `upstream_oauth.py login` again and restart Claude.

**The provider says the redirect URI doesn't match.** Register exactly `http://127.0.0.1/callback` (not `localhost`). If the provider wants the port too, set `redirectPort` and register that port.

**A registry server shows as failed in Claude Desktop.** Its log says why. `[launch] ... isn't installed` means the runtime it needs (Node.js, uv, Docker) isn't on PATH: install it and restart Desktop. `Missing secret` means the keychain entry is gone: reinstall the server.

**claude.ai can't connect but Claude Code can.** claude.ai connects from Anthropic's servers, so a `127.0.0.1` URL only works on your PC. Use the tunnel URL or your host's URL.

**Changes don't show up.** Claude Desktop only reads the config at startup, and closing the window leaves it running in the tray. Quit it fully.

## Tests

```powershell
uv add --dev pytest     # once
uv run pytest -q
```

The suite (`tests/`) runs offline in about a second. It covers how requests are built and where auth goes, the SSRF and secret-leak guards, response trimming, the built-in OAuth server (tampered, reused and cross-server tokens), upstream OAuth refresh and rotation (including two Claude clients sharing one sign-in), the OpenAPI importer, registry install plans, config writes and backups, and the UI's Host, token and cross-site checks. APIs are mocked and the keychain is in memory, so nothing touches your real config or secrets. GitHub Actions runs it on Windows and Linux for every push.

## Built with

Python 3.13, [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 2.x (low-level `Server`), `httpx`, `keyring`, `PyYAML`, `uv`. Targets MCP spec 2026-07-28; the SDK also serves clients still on 2025-11-25.

## Roadmap

1. ~~Thin slice: one API, API-key auth, local stdio, Claude Desktop~~
2. ~~OpenAPI import: generate `project.json` from a spec, pick operations, edit descriptions~~
3. ~~Built-in tester: call each tool from the UI and see the raw result~~
4. ~~Claude Code adapter~~
5. ~~Remote deployment: Streamable HTTP, Docker, static bearer auth~~
6. ~~OAuth: inbound sign-in for claude.ai, and upstream sign-in to APIs as you~~
7. MCP Registry servers (~~install from the registry~~), more clients, MCPB export

The browser UI (`gui.py`) gets each of these as it lands.
