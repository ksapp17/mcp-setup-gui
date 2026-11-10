# mcp-setup-gui

Turn an HTTP API into tools Claude can use, without writing server code or hand-editing config files.

You describe an API in `project.json`. A generic MCP runtime serves every endpoint you list as a tool, and an installer registers it with Claude Desktop safely: it verifies the server first, shows a diff, backs up the config, and can undo the change.

**Status:** milestones 1 and 2 of 7 work: local stdio servers for Claude Desktop, API-key and bearer auth, and import from OpenAPI specs. A browser UI, remote deployment and more clients are planned (see [Roadmap](#roadmap)).

```
project.json ──► runtime.py ──stdio──► Claude Desktop ──► Claude
     ▲               │
     │               └──► upstream API (key from the OS keychain)
 install.py ──► claude_desktop_config.json (backup, diff, atomic write)
```

## Why

Connecting Claude to an API today means writing an MCP server, then editing a JSON config file by hand. That config file is shared with the app's own settings, sits in different places on different installs, and one missing comma silently disables every server. API keys usually end up pasted into that file in plain text.

This project makes the server a data file and the install a checked, reversible operation.

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

Supported: OpenAPI 3.0 and 3.1, JSON or YAML, path/query/header parameters, JSON request bodies, API-key (header or query) and bearer auth. Not yet: Swagger 2.0 (convert it first), external `$ref`s, form and multipart bodies (those operations are skipped and listed), cookie auth, basic auth, OAuth (configured as a bearer token you paste, until milestone 6).

After importing, read the tool descriptions in the project file. The importer lists any that are short, and the description is what Claude uses to decide when to call a tool.

## Commands

| Command | What it does |
| --- | --- |
| `uv run install.py add --project project.json` | Verify and register the project with Claude Desktop |
| `uv run install.py remove --project project.json` | Remove the entry this installer added |
| `uv run install.py list` | Show every MCP server in each Claude Desktop config found |
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
| `paramLocations` | For each argument: `"path"` (fills `{name}` in `path`), `"query"`, `"header"`, `"body"` (a field of the JSON body) or `"bodyRaw"` (the whole body). The object form `{"in": "body", "name": "id"}` lets an argument's name differ from the API's. |
| `inputSchema` | JSON Schema for the tool's arguments, passed to Claude as-is |
| `description` | What Claude reads to decide when to call the tool. The main quality lever. |
| `annotations` | MCP tool hints, e.g. `readOnlyHint` |
| `enabled` | Optional, defaults to `true` |
| `responseByteCap` | Responses longer than this are truncated (default 50,000 bytes) |

Adding a tool is a JSON edit and a Claude Desktop restart. No code changes.

## Security design

- **Secrets live in the OS keychain only.** `project.json`, the Claude Desktop config, the repo and the logs hold a `secret://` reference, never the value. The runtime reads the keychain itself at startup.
- **Errors never leak the key.** Network errors are reported by type only, because the request URL contains the key when it's a query parameter.
- **Requests stay on the configured host.** Path arguments are URL-encoded and the final URL's host is checked against `baseUrl`, so a tool argument can't redirect a request elsewhere. Auth is applied after header arguments, so an argument can't replace it.
- **Writes are opt-in.** The importer pre-selects only read-only operations and marks DELETE operations with `destructiveHint`, so clients can ask before running them.
- **Config writes are checked and reversible.** The installer refuses to touch a config it can't parse, shows a diff, backs up to `~/.mcpgui/backups`, writes to a temp file and swaps it in, re-reads the result, and restores the backup if it doesn't match.
- **Nothing is installed blind.** Before writing, the installer starts the runtime exactly as Claude Desktop will and lists the tools Claude is about to get.
- **Merges, never overwrites.** Other servers and the app's own settings are left untouched. Replacing an entry the installer didn't create needs explicit confirmation.

## Troubleshooting

**Claude Desktop shows "No servers added."** It's reading a different config file, or it hasn't restarted. Check *Settings → Desktop app → Extensions → Edit config* to see which file it uses. On some Windows installs that's `%LOCALAPPDATA%\Claude-Data\claude_<id>\claude_desktop_config.json`, not the `%APPDATA%\Claude` path most guides give. `install.py` searches both.

**The server shows as failed.** Read its log. The runtime writes errors to stderr, which Claude Desktop records per server (`mcp-server-<name>.log`).

**Changes don't show up.** Claude Desktop only reads the config at startup, and closing the window leaves it running in the tray. Quit it fully.

## Built with

Python 3.13, [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 2.x (low-level `Server`), `httpx`, `keyring`, `PyYAML`, `uv`. Targets MCP spec 2026-07-28; the SDK also serves clients still on 2025-11-25.

## Roadmap

1. ~~Thin slice: one API, API-key auth, local stdio, Claude Desktop~~
2. ~~OpenAPI import: generate `project.json` from a spec, pick operations~~ (description editing comes with the UI)
3. Built-in tester: call each tool from the UI and see the raw JSON-RPC
4. Claude Code adapter
5. Remote deployment: Streamable HTTP, Docker, static bearer auth
6. OAuth upstream and inbound
7. MCP Registry servers, more clients, MCPB export

A browser wizard (Python, NiceGUI) wraps each of these as it lands. For now the CLI is the interface.