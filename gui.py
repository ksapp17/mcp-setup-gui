"""Browser UI for mcp-setup-gui.

Run:  uv run gui.py            (add --no-browser to skip opening a tab)

It serves pages on 127.0.0.1 only and opens your browser with a one-time link.
Everything it does goes through the same code as the command-line tools:
import_openapi.py for importing, install.py for installing, runtime.py for testing.

Pages are plain HTML forms handled in Python. There is no JavaScript.
"""

import argparse
import copy
import os
import re
import hashlib
import html
import json
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import anyio
import keyring
import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

import claude_code
import import_openapi as importer
import install as installer
import remote
from runtime import truncate
import registry
import upstream_oauth as upstream

HERE = Path(__file__).resolve().parent
KEYCHAIN_SERVICE = "mcp-setup-gui"
COOKIE = "mcpgui_session"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
TOKEN = secrets.token_urlsafe(24)
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
DRAFTS = {}  # import drafts waiting for the user to pick operations; in memory only


class UserError(Exception):
    """Something the user can fix. Shown on the page instead of a stack trace."""


# ---------------------------------------------------------------- security ---

def same(a, b):
    return secrets.compare_digest(str(a).encode(), str(b).encode())


async def guard(request: Request, call_next):
    # 1. DNS rebinding: a malicious site can point its own domain at 127.0.0.1,
    #    but it can't make the browser send our Host header.
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return PlainTextResponse("Forbidden: unexpected Host header.", status_code=403)

    # 2. The one-time link from the terminal sets a session cookie, then drops the token from the URL.
    token = request.query_params.get("t")
    if token is not None and same(token, TOKEN):
        response = RedirectResponse(request.url.path, status_code=303)
        response.set_cookie(COOKIE, TOKEN, httponly=True, samesite="strict")
        return response

    # 3. Every other request needs that cookie. Other sites can't read or send it.
    if not same(request.cookies.get(COOKIE, ""), TOKEN):
        return PlainTextResponse(
            "Not signed in. Open the link printed in the terminal where gui.py is running.",
            status_code=403)

    # 4. CSRF: form posts must come from our own pages.
    if request.method == "POST" and request.headers.get("origin") not in ALLOWED_ORIGINS:
        return PlainTextResponse("Forbidden: cross-site request.", status_code=403)

    response = await call_next(request)
    # No scripts at all, no framing, forms may only post back here
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"  # pages can show tokens; keep them out of the browser cache
    response.headers["Referrer-Policy"] = "same-origin"  # "no-referrer" makes Chrome send Origin: null on form posts
    return response


# ------------------------------------------------------------------ helpers ---

e = html.escape

STYLE = """
* { box-sizing: border-box; }
body { font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0; color: #1d1d1f; background: #f6f6f4; }
header { background: #1d1d1f; color: #fff; padding: 12px 28px; }
header a { color: #fff; text-decoration: none; font-weight: 600; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 28px 60px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 32px 0 10px; } h3 { font-size: 15px; margin: 16px 0 6px; }
.sub { color: #666; margin: 0 0 16px; }
.card { background: #fff; border: 1px solid #e2e2de; border-radius: 10px; padding: 18px 20px; margin: 14px 0; }
table { border-collapse: collapse; width: 100%; background: #fff; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #ecece8; vertical-align: top; }
th { font-size: 13px; color: #555; font-weight: 600; background: #fafaf8; }
label { display: block; font-weight: 600; font-size: 13px; margin: 12px 0 4px; }
input[type=text], input[type=password], select, textarea { width: 100%; padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font: inherit; background: #fff; }
textarea { min-height: 64px; font-size: 14px; }
textarea.code, pre { font: 13px/1.45 ui-monospace, Consolas, monospace; }
pre { background: #1d1d1f; color: #eaeaea; padding: 14px; border-radius: 8px; overflow-x: auto; white-space: pre-wrap; }
button { background: #1d1d1f; color: #fff; border: 0; border-radius: 6px; padding: 9px 16px; font: inherit; font-weight: 600; cursor: pointer; margin-top: 14px; }
button.secondary { background: #fff; color: #1d1d1f; border: 1px solid #bbb; }
button.danger { background: #b42318; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; align-items: end; }
.tag { display: inline-block; font-size: 12px; padding: 1px 7px; border-radius: 10px; background: #eee; margin-right: 4px; }
.ok { background: #e3f4e1; color: #1e6b1a; } .warn { background: #fdf0d5; color: #8a5a00; } .bad { background: #fde3e1; color: #a1170c; }
.msg { padding: 10px 14px; border-radius: 8px; margin: 0 0 16px; }
.msg.info { background: #e3f4e1; } .msg.error { background: #fde3e1; }
.muted { color: #777; font-size: 13px; }
code { font: 13px ui-monospace, Consolas, monospace; background: #f0f0ec; padding: 1px 4px; border-radius: 4px; }
"""


def page(title, body, request=None, error=None):
    msg = request.query_params.get("msg") if request else None
    banner = ""
    if error:
        banner = f'<div class="msg error">{e(str(error))}</div>'
    elif msg:
        banner = f'<div class="msg info">{e(msg)}</div>'
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{e(title)} · MCP Setup</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>{STYLE}</style></head>
<body><header><a href="/">MCP Setup</a></header><main>{banner}{body}</main></body></html>""")


def go(path, **params):
    query = "?" + urlencode(params) if params else ""
    return RedirectResponse(path + query, status_code=303)


def handled(endpoint):
    """Turn errors the user can fix into a readable page."""
    async def wrapper(request):
        try:
            return await endpoint(request)
        except (UserError, installer.InstallError, importer.SpecError) as err:
            back = request.headers.get("referer") or "/"
            return page("Problem", f'<p><a href="{e(back)}">Back</a></p>', request, error=err)
    return wrapper


def list_projects():
    found = []
    for path in sorted(HERE.glob("*.json")) + sorted((HERE / "examples").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("apis"), list) and "name" in data:
            found.append((path.relative_to(HERE).as_posix(), data))
    return found


def open_project(rel):
    # Only files we listed ourselves: no path tricks through the form
    for r, data in list_projects():
        if r == rel:
            return HERE / r, data
    raise UserError(f"No project file called {rel!r} in this folder.")


def secret_stored(api):
    auth = api.get("auth")
    if not auth:
        return None
    account = auth["secretRef"].removeprefix("secret://")
    return keyring.get_password(KEYCHAIN_SERVICE, account) is not None


def desktop_status(path, data):
    """Where (if anywhere) this project is installed in Claude Desktop."""
    for config in installer.candidate_configs():
        try:
            servers = installer.read_json(config).get("mcpServers", {})
        except (OSError, json.JSONDecodeError):
            continue
        entry = servers.get(data["name"])
        if entry and str(path) in [str(a) for a in entry.get("args", [])]:
            return "installed", config
        if entry:
            return "conflict", config
    return "not installed", None


def status_tag(state):
    css = {"installed": "ok", "conflict": "bad"}.get(state, "")
    text = {"installed": "Installed in Claude Desktop", "conflict": "Name used by another entry",
            "not installed": "Not installed"}[state]
    return f'<span class="tag {css}">{text}</span>'


def args_summary(tool):
    schema = tool.get("inputSchema", {})
    required = set(schema.get("required", []))
    names = [f"{n}*" if n in required else n for n in schema.get("properties", {})]
    return ", ".join(names) or "none"


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -------------------------------------------------------------------- home ---

@handled
async def home(request):
    projects = list_projects()
    rows = ""
    for rel, data in projects:
        tools = [t for a in data["apis"] for t in a.get("tools", [])]
        enabled = sum(1 for t in tools if t.get("enabled", True))
        state, _ = desktop_status(HERE / rel, data)
        rows += (f'<tr><td><a href="/project?{urlencode({"file": rel})}"><b>{e(data["name"])}</b></a></td>'
                 f'<td><code>{e(rel)}</code></td><td>{len(data["apis"])}</td>'
                 f'<td>{enabled} of {len(tools)}</td><td>{status_tag(state)}</td></tr>')
    table = (f"<table><tr><th>Project</th><th>File</th><th>APIs</th><th>Tools enabled</th><th>Claude Desktop</th></tr>{rows}</table>"
             if rows else '<p class="muted">No projects yet. Import an API below.</p>')

    targets = '<option value="__new__">New project</option>' + "".join(
        f'<option value="{e(rel)}">Add to {e(data["name"])} ({e(rel)})</option>' for rel, data in projects)

    body = f"""
<h1>Projects</h1><p class="sub">Each project becomes one MCP server in Claude.
Or <a href="/registry">install a ready-made server from the MCP Registry</a>.</p>
{table}
<h2>Import an API</h2>
<form class="card" method="post" action="/import">
  <label>OpenAPI spec: URL or file path</label>
  <input type="text" name="spec" placeholder="https://petstore3.swagger.io/api/v3/openapi.json" required>
  <div class="row">
    <div><label>Add to</label><select name="target">{targets}</select></div>
    <div><label>New project name <span class="muted">(new projects only; defaults to the spec title)</span></label>
      <input type="text" name="new_name" placeholder="petstore"></div>
    <div><label>API id <span class="muted">(optional)</span></label><input type="text" name="api_id"></div>
    <div><label>Base URL <span class="muted">(only if the spec's is relative)</span></label><input type="text" name="base_url"></div>
  </div>
  <label>Only show operations matching <span class="muted">(optional; path, name or tag)</span></label>
  <input type="text" name="filter">
  <button>Read spec</button>
</form>"""
    return page("Projects", body, request)


# ------------------------------------------------------------------ import ---

@handled
async def start_import(request):
    form = await request.form()
    source = form.get("spec", "").strip()
    if not source:
        raise UserError("Enter a spec URL or file path.")
    if not source.lower().startswith(("http://", "https://")):
        source = str((HERE / source).resolve()) if not Path(source).is_absolute() else source

    raw = await run_in_threadpool(importer.load_spec, source)
    spec = importer.deref(raw, raw)
    title = (spec.get("info") or {}).get("title") or "api"

    target = form.get("target", "__new__")
    if target == "__new__":
        project_name = importer.slug(form.get("new_name", "").strip() or title)
        out = HERE / f"{project_name}.json"
        if out.exists():
            raise UserError(f"{out.name} already exists. Choose it under 'Add to', or pick another name.")
    else:
        out, data = open_project(target)
        project_name = data["name"]
    api_id = importer.slug(form.get("api_id", "").strip() or title)

    warnings = []
    url = importer.base_url(spec, source, form.get("base_url", "").strip() or None)
    auth, scheme = importer.detect_auth(spec, project_name, api_id, warnings)
    candidates, skipped = importer.candidate_tools(spec, warnings)

    needle = form.get("filter", "").strip().lower()
    if needle:
        candidates = [c for c in candidates
                      if needle in c["tool"]["path"].lower() or needle in c["tool"]["name"]
                      or any(needle in str(t).lower() for t in c["tags"])]
    if not candidates:
        raise UserError("No operations to import" + (" match that filter." if needle else "."))

    existing_ids = [a.get("id") for a in json.loads(out.read_text(encoding="utf-8"))["apis"]] if out.exists() else []
    draft_id = uuid.uuid4().hex
    DRAFTS[draft_id] = {
        "title": title, "url": url, "auth": auth, "scheme": scheme, "candidates": candidates,
        "skipped": skipped, "warnings": warnings, "out": out, "project_name": project_name,
        "api_id": api_id, "replaces": api_id in existing_ids,
    }
    return go(f"/draft/{draft_id}")


def get_draft(request):
    draft = DRAFTS.get(request.path_params["draft_id"])
    if not draft:
        raise UserError("That import has expired (the GUI was restarted). Start the import again.")
    return draft


async def render_draft(request, error=None):
    draft = get_draft(request)
    if draft["auth"]:
        p = draft["auth"]["placement"]
        auth = (f'<code>{e(draft["scheme"])}</code>, sent in the {e(p["in"])} <code>{e(p["name"])}</code>'
                + (f' as <code>{e(p["prefix"])}&lt;token&gt;</code>' if p.get("prefix") else ""))
    else:
        auth = "none"
    notes = "".join(f"<li>{e(n)}</li>" for n in draft["warnings"] + draft["skipped"])
    notes = f'<div class="card"><b>Notes</b><ul>{notes}</ul></div>' if notes else ""

    rows = ""
    for i, c in enumerate(draft["candidates"]):
        t = c["tool"]
        read_only = t["annotations"].get("readOnlyHint", False)
        checked = "checked" if read_only and not c["deprecated"] else ""
        flags = ""
        if t["annotations"].get("destructiveHint"):
            flags += '<span class="tag bad">destructive</span>'
        elif not read_only:
            flags += '<span class="tag warn">writes</span>'
        if c["deprecated"]:
            flags += '<span class="tag">deprecated</span>'
        short = len(t["description"]) < importer.WEAK_DESCRIPTION
        hint = '<div class="muted">Short description: say when Claude should use this.</div>' if short else ""
        rows += f"""<tr>
  <td><input type="checkbox" name="sel" value="{i}" {checked}></td>
  <td><b>{e(t["name"])}</b><br><span class="muted">{e(t["method"])} {e(t["path"])}</span><br>{flags}
      <div class="muted">Args: {e(args_summary(t))}</div></td>
  <td><textarea name="desc_{i}">{e(t["description"])}</textarea>{hint}</td></tr>"""

    replace = ""
    if draft["replaces"]:
        replace = (f'<label><input type="checkbox" name="force" value="1"> Replace the API '
                   f'<code>{e(draft["api_id"])}</code> already in {e(draft["out"].name)}</label>')

    body = f"""
<h1>{e(draft["title"])}</h1>
<p class="sub">Base URL <code>{e(draft["url"])}</code> · Auth: {auth} · Writing to <code>{e(draft["out"].name)}</code></p>
{notes}
<form method="post">
<p class="muted">Read-only operations are pre-selected. The description is what Claude reads to decide
when to call a tool, so edit any that are vague. More than {importer.TOOL_WARN_COUNT} tools makes it harder
for Claude to pick the right one.</p>
<table><tr><th></th><th>Operation</th><th>Description</th></tr>{rows}</table>
{replace}
<button>Write project</button>
</form>"""
    return page(draft["title"], body, request, error=error)


@handled
async def save_draft(request):
    draft = get_draft(request)
    form = await request.form()
    picked = sorted({int(v) for v in form.getlist("sel") if v.isdigit()})
    picked = [i for i in picked if i < len(draft["candidates"])]
    if not picked:
        return await render_draft(request, error="Select at least one operation.")

    tools = []
    for i in picked:
        tool = copy.deepcopy(draft["candidates"][i]["tool"])
        tool["description"] = " ".join(form.get(f"desc_{i}", "").split()) or tool["description"]
        tools.append(tool)

    api = {"id": draft["api_id"], "baseUrl": draft["url"], "source": "openapi"}
    if draft["auth"]:
        api["auth"] = draft["auth"]
    api["tools"] = tools
    importer.write_project(draft["out"], api, draft["project_name"], bool(form.get("force")))
    DRAFTS.pop(request.path_params["draft_id"], None)

    rel = draft["out"].relative_to(HERE).as_posix()
    msg = f"Wrote {len(tools)} tool(s) to {rel}."
    if draft["auth"] and draft["auth"].get("oauth2"):
        msg += " Fill in the OAuth app's client ID and secret below, then sign in."
    elif draft["auth"]:
        msg += " Store the API's secret below, then install."
    return go("/project", file=rel, msg=msg)


# ----------------------------------------------------------------- project ---

@handled
async def show_project(request):
    rel = request.query_params.get("file", "")
    path, data = open_project(rel)
    state, where = desktop_status(path, data)

    apis_html = ""   # API cards with their secret forms (must sit outside the tools form)
    tables_html = ""  # tool tables, inside the one "Save tools" form
    tool_options = ""
    for a_i, api in enumerate(data["apis"]):
        stored = secret_stored(api)
        if upstream.oauth_config(api):
            secret_form, auth_line = oauth_card(rel, api)
        elif api.get("auth"):
            p = api["auth"]["placement"]
            secret_tag = '<span class="tag ok">secret stored</span>' if stored else '<span class="tag bad">secret missing</span>'
            secret_form = f"""
<form method="post" action="/secret" class="row">
  <input type="hidden" name="file" value="{e(rel)}"><input type="hidden" name="api" value="{e(api["id"])}">
  <div><label>{"Replace" if stored else "Store"} the secret <span class="muted">(kept in the OS keychain, never in a file)</span></label>
  <input type="password" name="value" autocomplete="off" required></div>
  <div><button class="secondary">Save to keychain</button></div>
</form>"""
            auth_line = (f'Auth: {e(p["in"])} <code>{e(p["name"])}</code>'
                         + (f' as <code>{e(p["prefix"])}&lt;token&gt;</code>' if p.get("prefix") else "")
                         + f' · {secret_tag}')
        else:
            secret_form, auth_line = "", "Auth: none"

        rows = ""
        for t_i, tool in enumerate(api.get("tools", [])):
            on = "checked" if tool.get("enabled", True) else ""
            if on:
                tool_options += f'<option value="{e(tool["name"])}">{e(tool["name"])}</option>'
            rows += f"""<tr>
  <td><input type="checkbox" name="en_{a_i}_{t_i}" value="1" {on}></td>
  <td><b>{e(tool["name"])}</b><br><span class="muted">{e(tool["method"])} {e(tool["path"])}</span>
      <div class="muted">Args: {e(args_summary(tool))}</div></td>
  <td><textarea name="desc_{a_i}_{t_i}">{e(tool.get("description", ""))}</textarea>
      <div class="row"><div><label>Keep only these response fields <span class="muted">(optional, comma-separated, e.g. name, types.type.name)</span></label>
      <input type="text" name="fields_{a_i}_{t_i}" value="{e(", ".join(tool.get("responseFields") or []))}"></div>
      <div><label>Max items per list <span class="muted">(optional)</span></label>
      <input type="text" name="max_{a_i}_{t_i}" value="{e(str(tool.get("responseMaxItems") or ""))}" inputmode="numeric"></div></div></td></tr>"""
        apis_html += f"""
<div class="card"><b>{e(api["id"])}</b> <span class="muted"><code>{e(api["baseUrl"])}</code></span>
<div>{auth_line}</div>{secret_form}</div>"""
        tables_html += f"""
<h3>{e(api["id"])}</h3>
<table><tr><th>On</th><th>Tool</th><th>Description</th></tr>{rows}</table>"""

    configs = installer.candidate_configs()
    config_options = "".join(
        f'<option value="{e(str(c))}" {"selected" if where and c == where else ""}>{e(str(c))}</option>'
        for c in configs)
    if configs:
        config_field = f'<label>Claude Desktop config</label><select name="config">{config_options}</select>'
    else:
        config_field = ('<p class="muted">No Claude Desktop config found. In Claude Desktop open Settings, '
                        'Desktop app, Extensions, Edit config to create it, or paste its path here.</p>')
    config_field += ('<label>Or a config path <span class="muted">(optional)</span></label>'
                     '<input type="text" name="custom_config">')

    body = f"""
<h1>{e(data["name"])}</h1>
<p class="sub"><code>{e(rel)}</code> · {status_tag(state)}</p>

<h2>APIs</h2>
{apis_html}

<h2>Tools</h2>
<form method="post" action="/project/save">
<input type="hidden" name="file" value="{e(rel)}">
{tables_html}
<button>Save tools</button> <span class="muted">Claude Desktop picks up changes after a restart.</span>
</form>

<h2>Test a tool</h2>
<form class="card" method="post" action="/test">
  <input type="hidden" name="file" value="{e(rel)}">
  <label>Tool</label><select name="tool">{tool_options}</select>
  <label>Arguments (JSON) <span class="muted">* marks required arguments in the list above</span></label>
  <textarea class="code" name="args">{{}}</textarea>
  <button>Run</button>
  <p class="muted">Starts the runtime the same way Claude Desktop does and calls the tool once.</p>
</form>

<h2>Claude Desktop</h2>
<form class="card" method="post" action="/install/preview">
  <input type="hidden" name="file" value="{e(rel)}">
  {config_field}
  <p class="muted">Quit Claude Desktop before writing. It saves its own settings to the same file.</p>
  <button name="action" value="add">Preview install</button>
  <button class="secondary" name="action" value="remove">Preview removal</button>
</form>

<h2>Claude Code</h2>
{code_card(rel, data)}

<h2>Remote server</h2>
<p class="sub">For claude.ai on the web, the Claude mobile apps, and connectors in Claude Desktop.
They connect from Anthropic's servers, so the runtime has to run somewhere public over HTTPS.</p>
{remote_card(rel, data)}"""
    return page(data["name"], body, request)


def remote_card(rel, data):
    token_ok = remote.get_token(data) is not None
    url = (data.get("deployment") or {}).get("url") or ""
    hidden = f'<input type="hidden" name="file" value="{e(rel)}">'

    if token_ok:
        token_html = f"""<span class="tag ok">token stored in keychain</span>
<form method="post" action="/remote/token">{hidden}
  <button class="secondary" name="action" value="show">Show token</button>
  <button class="secondary" name="action" value="replace">Replace token</button>
</form>"""
    else:
        token_html = f"""<span class="tag bad">no token yet</span>
<form method="post" action="/remote/token">{hidden}<button name="action" value="create">Create token</button></form>"""

    env_rows = "".join(f"<tr><td><code>{e(n)}</code></td><td>{e(w)}</td></tr>" for n, _, w in remote.host_env(data))
    docker = "\n".join(remote.docker_commands(data, rel))
    local_try = ("cloudflared tunnel --url http://127.0.0.1:8000\n"
                 "   (save the https URL it prints, plus /mcp, in step 3; then:)\n"
                 f"uv run remote.py serve --project {rel}")

    tool_options = "".join(f'<option value="{e(t["name"])}">{e(t["name"])}</option>'
                           for a in data["apis"] for t in a.get("tools", []) if t.get("enabled", True))
    remote_recorded = [c for c in data.get("clients", [])
                       if c.get("client") == claude_code.CLIENT_ID and c.get("remote")]
    code_status = "".join(
        f'<span class="tag ok">{e(c["scope"])} scope' + (f': {e(c["folder"])}' if c.get("folder") else "")
        + "</span>" for c in remote_recorded) or '<span class="tag">Not registered</span>'

    connect = ""
    if url:
        code_form = ""
        if shutil.which("claude"):
            code_form = f"""
<form class="card" method="post" action="/code/preview">{hidden}<input type="hidden" name="remote" value="1">
  <b>Claude Code</b> <div>{code_status}</div>
  <div class="row">
    <div><label>Scope</label><select name="scope">
      <option value="user">user: every project on this machine</option>
      <option value="local">local: one folder, only you</option></select></div>
    <div><label>Project folder <span class="muted">(local scope only)</span></label><input type="text" name="folder"></div>
  </div>
  <p class="muted">Registers <code>{e(claude_code.remote_name(data["name"]))}</code> pointing at your URL, with the token as a header.</p>
  <button name="action" value="add">Preview install</button>
  <button class="secondary" name="action" value="remove">Preview removal</button>
</form>"""
        local_note = ""
        if remote.is_local_url(url):
            local_note = ('<div class="msg error">claude.ai connects from Anthropic\'s servers, so it can\'t reach '
                          f'<code>{e(url)}</code>. Use the tunnel or your host\'s https URL for it. '
                          'Claude Code on this PC can use the local URL.</div>')
        connect = f"""
<div class="card"><b>4. Test it</b>
<form method="post" action="/remote/test">{hidden}
  <div class="row"><div><label>Tool</label><select name="tool">{tool_options}</select></div>
  <div><label>Arguments (JSON)</label><input type="text" name="args" value="{{}}"></div></div>
  <button>Call the remote server</button>
</form></div>
<div class="card"><b>5. Connect Claude</b>
{local_note}
<p><b>claude.ai, Claude mobile and Claude Desktop</b>, with sign-in (works in every account): run the server with
OAuth on, which <code>remote.py serve</code> does using the URL saved above. In claude.ai open Settings, Connectors,
Add custom connector, and enter <code>{e(url)}</code>. Keep Authentication on <b>Sign in now</b>, choose
<b>dynamic client registration</b> (DCR) for the OAuth client, then Add and Connect. Your server's sign-in page opens:
paste your token (Show token above) and click Allow.</p>
<p><b>Or with a header</b> (only in accounts with the Request headers beta): Authentication <b>No sign-in</b>, then
Request headers <code>x-api-key</code> with your token as the value. Connectors you add there also appear in the mobile apps
and in Claude Desktop. Request headers are a beta and may not show in every account yet; OAuth (milestone 6) is the
alternative.</p>
</div>
{code_form}"""

    return f"""
<div class="card"><b>1. Token</b> <span class="muted">Clients must send it; the server rejects everything else.</span>
<div>{token_html}</div></div>

<div class="card"><b>2. Run it somewhere public</b>
<p class="muted">The server needs these environment variables. If you change the URL (a new tunnel, a new host),
update <code>MCP_PUBLIC_URL</code> and restart the server, since sign-in links point at it. <code>uv run remote.py env --project {e(rel)} --values</code>
prints their values for pasting into a host's secret settings.</p>
<table><tr><th>Variable</th><th>What it is</th></tr>{env_rows}</table>
<p><b>Any host that runs containers</b> (Render, Railway, Fly.io, a VPS) using the Dockerfile in this folder:</p>
<pre>{e(docker)}</pre>
<p><b>Or try it from this PC first</b> with a free Cloudflare quick tunnel, which gives you a temporary
https URL. Start the tunnel first, since sign-in needs its URL. Run each in its own terminal and stop both when you're done.</p>
<pre>{e(local_try)}</pre></div>

<div class="card"><b>3. Server URL</b>
<form method="post" action="/remote/url">{hidden}
  <label>Public URL, ending in /mcp</label>
  <input type="text" name="url" value="{e(url)}" placeholder="https://my-server.example.com/mcp">
  <button class="secondary">Save URL</button>
</form></div>
{connect}"""


def code_card(rel, data):
    recorded = [c for c in data.get("clients", [])
                if c.get("client") == claude_code.CLIENT_ID and not c.get("remote")]
    status = "".join(
        f'<span class="tag ok">{e(c["scope"])} scope'
        + (f': {e(c["folder"])}' if c.get("folder") else ": all projects") + "</span>"
        for c in recorded) or '<span class="tag">Not registered</span>'
    if not shutil.which("claude"):
        return (f'<div class="card">{status}<p class="muted">The <code>claude</code> command isn\'t on PATH, '
                "so Claude Code can't be set up from here. Install Claude Code, then restart gui.py.</p></div>")
    last = recorded[-1] if recorded else {}
    options = "".join(
        f'<option value="{s}" {"selected" if last.get("scope", "user") == s else ""}>{label}</option>'
        for s, label in (("user", "user: every project on this machine"),
                         ("local", "local: one folder, only you"),
                         ("project", "project: one folder, shared via .mcp.json")))
    return f"""
<form class="card" method="post" action="/code/preview">
  <input type="hidden" name="file" value="{e(rel)}">
  <div>{status}</div>
  <div class="row">
    <div><label>Scope</label><select name="scope">{options}</select></div>
    <div><label>Project folder <span class="muted">(local and project scope only)</span></label>
      <input type="text" name="folder" value="{e(last.get("folder") or "")}" placeholder="C:\\Users\\you\\code\\my-repo"></div>
  </div>
  <p class="muted">Runs Claude Code's own <code>claude mcp add</code> / <code>remove</code>. New Claude Code sessions pick it up.</p>
  <button name="action" value="add">Preview install</button>
  <button class="secondary" name="action" value="remove">Preview removal</button>
</form>"""


@handled
async def save_project(request):
    form = await request.form()
    rel = form.get("file", "")
    path, data = open_project(rel)
    for a_i, api in enumerate(data["apis"]):
        for t_i, tool in enumerate(api.get("tools", [])):
            tool["enabled"] = form.get(f"en_{a_i}_{t_i}") == "1"
            desc = " ".join(form.get(f"desc_{a_i}_{t_i}", "").split())
            if desc:
                tool["description"] = desc
            fields = [f.strip() for f in form.get(f"fields_{a_i}_{t_i}", "").replace("\n", ",").split(",") if f.strip()]
            if fields:
                bad = [f for f in fields if not re.fullmatch(r"[^.\s]+(\.[^.\s]+)*", f)]
                if bad:
                    raise UserError(f"{tool['name']}: {bad[0]!r} isn't a field path. Use names joined by dots, like stats.base_stat.")
                tool["responseFields"] = fields
            else:
                tool.pop("responseFields", None)
            cap = form.get(f"max_{a_i}_{t_i}", "").strip()
            if cap:
                if not cap.isdigit() or int(cap) < 1:
                    raise UserError(f"{tool['name']}: max items must be a whole number above 0.")
                tool["responseMaxItems"] = int(cap)
            else:
                tool.pop("responseMaxItems", None)
    installer.atomic_write(path, installer.to_text(data))
    return go("/project", file=rel, msg="Saved. Restart Claude Desktop if this project is installed.")


@handled
async def store_secret(request):
    form = await request.form()
    rel = form.get("file", "")
    _, data = open_project(rel)
    api = next((a for a in data["apis"] if a.get("id") == form.get("api")), None)
    if not api or not api.get("auth"):
        raise UserError("That API doesn't use a secret.")
    value = form.get("value", "")
    if not value.strip():
        raise UserError("The secret was empty, so nothing was stored.")
    account = api["auth"]["secretRef"].removeprefix("secret://")
    keyring.set_password(KEYCHAIN_SERVICE, account, value)
    return go("/project", file=rel, msg=f"Secret for {api['id']} saved to the OS keychain.")


# ---------------------------------------------------------- upstream oauth ---

LOGINS = {}  # (project file, api id) -> {"status": waiting|done|error, "message", "url", "flow"}


def oauth_card(rel, api):
    """Auth line and forms for an API that uses OAuth: client ID/secret, sign in, sign out."""
    cfg = upstream.oauth_config(api)
    stored = upstream.stored_tokens(api)
    login = LOGINS.get((rel, api["id"]))
    host = e(remote_host(cfg.get("authorizeUrl", "")))
    tag = ('<span class="tag ok">signed in</span>' if stored else '<span class="tag bad">not signed in</span>')
    auth_line = f"Auth: OAuth 2.0 via <code>{host}</code> · {tag}"
    if stored:
        auth_line += f'<div class="muted">{e(upstream.describe(stored).removeprefix("signed in, "))}</div>'

    hidden = f'<input type="hidden" name="file" value="{e(rel)}"><input type="hidden" name="api" value="{e(api["id"])}">'
    secret_ref = cfg.get("clientSecretRef")
    secret_saved = bool(secret_ref and keyring.get_password(KEYCHAIN_SERVICE, secret_ref.removeprefix("secret://")))
    secret_field = ""
    if secret_ref:
        secret_field = f"""<div><label>Client secret <span class="muted">({"stored; type to replace" if secret_saved else "not stored"}, kept in the OS keychain)</span></label>
  <input type="password" name="client_secret" autocomplete="off"></div>"""
    forms = f"""
<form method="post" action="/oauth/client" class="row">{hidden}
  <div><label>Client ID <span class="muted">(from the OAuth app you registered with the provider)</span></label>
  <input type="text" name="client_id" value="{e(cfg.get("clientId", ""))}" autocomplete="off"></div>{secret_field}
  <div><button class="secondary">Save</button></div>
</form>
<p class="muted">Register the app with redirect URL <code>http://127.0.0.1{e(str(":" + str(cfg["redirectPort"])) if cfg.get("redirectPort") else "")}{e(cfg.get("redirectPath", upstream.DEFAULT_REDIRECT_PATH))}</code>.</p>
<form method="post" action="/oauth/start" class="row">{hidden}
  <div><button>{"Sign in again" if stored else "Sign in"}</button></div>
</form>"""
    if stored:
        forms += f"""<form method="post" action="/oauth/logout" class="row">{hidden}
  <div><button class="secondary">Sign out</button></div></form>"""
    if login and login["status"] == "waiting":
        forms += (f'<div class="msg info">Waiting for the sign-in in your browser. If no tab opened, '
                  f'<a href="{e(login["url"])}" target="_blank" rel="noreferrer">open the sign-in page</a>. '
                  f'Then refresh this page.</div>')
    elif login and login["status"] == "error":
        forms += f'<div class="msg error">Last sign-in failed: {e(login["message"])}</div>'
    return forms, auth_line


def remote_host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname or "?"


def find_oauth_api(data, api_id):
    api = next((a for a in data["apis"] if a.get("id") == api_id), None)
    if not api or not upstream.oauth_config(api):
        raise UserError("That API doesn't use OAuth.")
    return api


@handled
async def oauth_client(request):
    form = await request.form()
    rel = form.get("file", "")
    path, data = open_project(rel)
    api = find_oauth_api(data, form.get("api"))
    cfg = api["auth"]["oauth2"]
    client_id = form.get("client_id", "").strip()
    if client_id != cfg.get("clientId", ""):
        cfg["clientId"] = client_id
        installer.atomic_write(path, installer.to_text(data))
    secret = form.get("client_secret", "").strip()
    if secret:
        if not cfg.get("clientSecretRef"):
            raise UserError("This API has no oauth2.clientSecretRef in project.json, so there's nowhere to keep a client secret.")
        keyring.set_password(KEYCHAIN_SERVICE, cfg["clientSecretRef"].removeprefix("secret://"), secret)
    return go("/project", file=rel, msg=f"OAuth app settings for {api['id']} saved.")


@handled
async def oauth_start(request):
    form = await request.form()
    rel = form.get("file", "")
    _, data = open_project(rel)
    api = find_oauth_api(data, form.get("api"))
    key = (rel, api["id"])
    old = LOGINS.get(key)
    if old and old["status"] == "waiting":
        old["flow"].cancel()  # its listener stops on its next tick, freeing a fixed redirect port
        await run_in_threadpool(old["thread"].join, 3)
    try:
        upstream.check_config(api)
        flow = upstream.LoginFlow(api, upstream.client_secret_for(api, rel),
                                  return_url=f"http://127.0.0.1:{PORT}/project?{urlencode({'file': rel})}")
        url = flow.start()
    except upstream.OAuthError as err:
        raise UserError(str(err))
    entry = {"status": "waiting", "message": "", "url": url, "flow": flow}
    LOGINS[key] = entry

    def finish():
        try:
            upstream.save_tokens(api, flow.wait())
            entry.update(status="done", message="")
        except Exception as err:  # OAuthError, keychain errors
            entry.update(status="error", message=str(err))

    entry["thread"] = threading.Thread(target=finish, daemon=True)
    entry["thread"].start()
    webbrowser.open(url)
    return go("/project", file=rel, msg=f"Sign-in for {api['id']} opened in a new tab. Approve it there, then refresh this page.")


@handled
async def oauth_logout(request):
    form = await request.form()
    rel = form.get("file", "")
    _, data = open_project(rel)
    api = find_oauth_api(data, form.get("api"))
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, api["auth"]["secretRef"].removeprefix("secret://"))
    except Exception:
        pass
    LOGINS.pop((rel, api["id"]), None)
    return go("/project", file=rel, msg=f"Signed out of {api['id']}. To revoke access too, remove the app on the provider's site.")


# ---------------------------------------------------------------- registry ---

PENDING = {}  # preview id -> planned registry install, values (including secrets) held in memory only


def registry_kinds(server):
    kinds = sorted({p.get("registryType", "?") for p in server.get("packages") or []})
    if server.get("remotes"):
        kinds.append("remote")
    return "".join(f'<span class="tag">{e(k)}</span>' for k in kinds) or '<span class="tag bad">nothing to install</span>'


def safe_link(url, text):
    url = str(url or "")
    if not url.startswith("https://"):
        return e(text)
    return f'<a href="{e(url)}" target="_blank" rel="noreferrer noopener">{e(text)}</a>'


def installed_html():
    rows = ""
    for local, spec in registry.installed():
        where = ", ".join(c["client"] + (f" ({c['scope']})" if c.get("scope") else "")
                          for c in spec.get("clients", [])) or "no client"
        rows += f"""<tr><td><b>{e(local)}</b></td><td><code>{e(spec.get("registryName", ""))}</code> {e(spec.get("version", ""))}</td>
<td>{e(where)}</td><td><form method="post" action="/registry/remove"><input type="hidden" name="local" value="{e(local)}">
<button class="secondary" style="margin:0">Remove</button></form></td></tr>"""
    if not rows:
        return ""
    return f"""<h2>Installed from the registry</h2>
<table><tr><th>Name in Claude</th><th>Server</th><th>Installed in</th><th></th></tr>{rows}</table>"""


@handled
async def registry_search(request):
    q = request.query_params.get("q", "").strip()
    results = ""
    if q:
        entries, more = await run_in_threadpool(registry.search, q)
        for entry in entries:
            srv = entry["server"]
            flag = "" if registry.status(entry) == "active" else f'<span class="tag warn">{e(registry.status(entry))}</span>'
            results += f"""<tr><td><a href="/registry/server?{urlencode({"name": srv["name"]})}"><b>{e(srv.get("title") or srv["name"])}</b></a>
<div class="muted"><code>{e(srv["name"])}</code> {e(srv.get("version", ""))}</div></td>
<td>{e(registry.short(srv.get("description"), 160))}</td><td>{registry_kinds(srv)}{flag}</td></tr>"""
        results = (f"<table><tr><th>Server</th><th>Description</th><th>Runs as</th></tr>{results}</table>"
                   + ('<p class="muted">More results exist: search for something more specific.</p>' if more else "")
                   if results else '<p class="muted">No servers match. The search looks at server names only.</p>')
    body = f"""
<h1>MCP Registry</h1>
<p class="sub">Ready-made servers from <code>{e(registry.REGISTRY_URL)}</code>. The registry lists them; it doesn't
review their code. Check who publishes a server before installing it.</p>
<form method="get" action="/registry" class="card">
  <label>Search by name</label><input type="text" name="q" value="{e(q)}" placeholder="github, filesystem, postgres..." required>
  <button>Search</button>
</form>
{results}{installed_html()}"""
    return page("MCP Registry", body, request)


def option_form(entry, opt):
    srv = entry["server"]
    if "unsupported" in opt:
        return f'<div class="card"><b>{e(opt["label"])}</b><p class="muted">Not available here: {e(opt["unsupported"])}.</p></div>'
    fields = ""
    for f in opt["fields"]:
        label = (f'{e(f["label"])}{" *" if f["required"] else ""}'
                 f'{" <span class=\"muted\">(secret, kept in the OS keychain)</span>" if f["secret"] else ""}')
        desc = f'<div class="muted">{e(f["description"])}</div>' if f["description"] else ""
        name = "f_" + f["key"]
        if f["choices"]:
            opts = "".join(f'<option {"selected" if c == f["default"] else ""}>{e(c)}</option>' for c in f["choices"])
            if not f["required"]:
                opts = '<option value=""></option>' + opts
            control = f'<select name="{e(name)}">{opts}</select>'
        else:
            kind = "password" if f["secret"] else "text"
            value = "" if f["secret"] else e(f["default"])
            control = (f'<input type="{kind}" name="{e(name)}" value="{value}" autocomplete="off" '
                       f'placeholder="{e(f["placeholder"])}"{" required" if f["required"] and not f["default"] else ""}>')
        fields += f"<label>{label}</label>{desc}{control}"

    needs = ""
    if opt["kind"] == "package":
        needs = (f'<span class="tag ok">{e(opt["needs"])} found</span>' if opt["found"]
                 else f'<span class="tag bad">needs {e(opt["needs"])}: not found on PATH</span>')
        clients = '<option value="claude-desktop">Claude Desktop</option><option value="claude-code">Claude Code (all projects)</option>'
    else:
        clients = '<option value="claude-code">Claude Code (all projects)</option>'
        needs = ('<p class="muted">For Claude Desktop or claude.ai, add the URL as a custom connector instead: '
                 'Settings → Connectors → Add custom connector.</p>')
    configs = "".join(f'<option value="{e(str(c))}">{e(str(c))}</option>' for c in installer.candidate_configs())
    config_field = (f'<label>Claude Desktop config <span class="muted">(Desktop only)</span></label><select name="config">{configs}</select>'
                    if opt["kind"] == "package" and configs else "")
    return f"""<form class="card" method="post" action="/registry/preview">
<b>{e(opt["label"])}</b> {needs}
<input type="hidden" name="name" value="{e(srv["name"])}"><input type="hidden" name="option" value="{e(opt["id"])}">
{fields or '<p class="muted">Nothing to fill in.</p>'}
<div class="row"><div><label>Name in Claude</label><input type="text" name="local" value="{e(registry.server_slug(srv["name"]))}" required></div>
<div><label>Install in</label><select name="client">{clients}</select></div></div>
{config_field}
<button>Preview</button></form>"""


@handled
async def registry_server(request):
    entry = await run_in_threadpool(registry.fetch, request.query_params.get("name", ""))
    srv = entry["server"]
    repo = (srv.get("repository") or {}).get("url")
    state = registry.status(entry)
    forms = "".join(option_form(entry, o) for o in registry.options(srv))
    body = f"""
<h1>{e(srv.get("title") or srv["name"])}</h1>
<p class="sub"><code>{e(srv["name"])}</code> {e(srv.get("version", ""))}
{"" if state == "active" else f'<span class="tag warn">{e(state)}</span>'}
{(" · source: " + safe_link(repo, repo)) if repo else ""} · <a href="/registry">Back</a></p>
<p>{e(srv.get("description", ""))}</p>
<h2>Install options</h2>{forms or '<p class="muted">This server lists no packages or URLs.</p>'}"""
    return page(srv.get("title") or srv["name"], body, request)


@handled
async def registry_preview(request):
    form = await request.form()
    entry = await run_in_threadpool(registry.fetch, form.get("name", ""))
    opt = registry.find_option(entry["server"], form.get("option"))
    local = registry.check_local_name(form.get("local", "").strip())
    client = form.get("client")
    values = {f["key"]: form.get("f_" + f["key"], "").strip() for f in opt["fields"]}

    if opt["kind"] == "remote":
        if client != "claude-code":
            raise UserError("Claude Desktop adds remote servers as connectors: Settings → Connectors → Add custom connector.")
        spec, headers = registry.plan_remote(entry, opt, values)
        secrets = {}
    else:
        spec, secrets = registry.plan_package(entry, opt, values, local)
        headers = {}

    pending = {"local": local, "spec": spec, "secrets": secrets, "headers": headers, "client": client}
    if client == "claude-desktop":
        config_path = chosen_config(form)
        original_text, config, new_config = await run_in_threadpool(registry.desktop_change, local, config_path)
        pending.update(config_path=config_path, digest=text_hash(original_text))
        change = f"<h2>Change to <code>{e(config_path.name)}</code></h2><pre>{e(installer.diff_text(config, new_config, config_path.name))}</pre>"
        if installer.desktop_is_running():
            change = ('<div class="msg error">Claude Desktop is running. Quit it first (tray icon, Quit), '
                      'or it may overwrite this change.</div>') + change
    elif client == "claude-code":
        change = f"<h2>Command</h2><pre>{e(claude_code.display(registry.code_args(local, spec, {k: '••••••••' for k in headers}, 'user')))}</pre>"
        if headers:
            change += '<div class="msg error">Claude Code keeps these headers in its own config file, in plain text.</div>'
    else:
        raise UserError("Pick where to install it.")

    launch = ""
    if "remote" not in spec:
        launch = (f"<h2>What will run</h2><p class=\"muted\">launch.py reads the secrets from the keychain and starts:</p>"
                  f"<pre>{e(' '.join(str(a) if isinstance(a, str) else '<secret>' for a in [spec['command'], *spec['args']]))}</pre>"
                  f"<p class=\"muted\">Plain settings: {e(json.dumps(spec['env'])) if spec['env'] else 'none'} · "
                  f"secrets: {e(', '.join(spec['secretEnv']) or 'none')}</p>")
    pid = uuid.uuid4().hex
    PENDING.clear()  # one install at a time; nothing secret lingers
    PENDING[pid] = pending
    body = f"""
<h1>Install {e(local)}</h1>
<p class="sub"><code>{e(spec["registryName"])}</code> {e(spec["version"])} · {e(spec["option"])} ·
<a href="/registry/server?{urlencode({"name": spec["registryName"]})}">Back</a></p>
{launch}{change}
<form method="post" action="/registry/apply"><input type="hidden" name="id" value="{pid}">
<button>Install</button> <span class="muted">Secrets go to the keychain; the config file is backed up first.</span></form>"""
    return page(f"Install {local}", body, request)


@handled
async def registry_apply(request):
    form = await request.form()
    pending = PENDING.pop(form.get("id", ""), None)
    if not pending:
        raise UserError("That preview has expired. Preview again.")
    local, spec = pending["local"], pending["spec"]
    if pending["client"] == "claude-desktop":
        config_path = pending["config_path"]
        original_text, config, new_config = await run_in_threadpool(registry.desktop_change, local, config_path)
        if not same(text_hash(original_text), pending["digest"]):
            raise UserError("The config file changed after the preview (Claude Desktop may have saved it). "
                            "Nothing was written. Preview again.")
        backup = await run_in_threadpool(registry.install_desktop, local, spec, pending["secrets"],
                                         config_path, original_text, new_config)
        msg = f"Installed {local} in Claude Desktop. Restart Desktop to load it. Backup: {backup}"
    else:
        await run_in_threadpool(registry.install_code, local, spec, pending["secrets"],
                                pending["headers"], "user", None)
        msg = f"Installed {local} in Claude Code. Start a new Claude Code session to load it."
    return go("/registry", msg=msg)


@handled
async def registry_remove(request):
    form = await request.form()
    local = registry.check_local_name(form.get("local", ""))
    notes = await run_in_threadpool(registry.remove, local)
    return go("/registry", msg=" ".join(notes + [f"Removed {local} and deleted its secrets."]))


# -------------------------------------------------------------------- test ---

def run_tool(project_path, tool_name, arguments):
    """Start the runtime like Claude Desktop does, call one tool, return (result, runtime stderr)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryFile("w+", encoding="utf-8") as errlog:
        async def call():
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(installer.RUNTIME), "--project", str(project_path)],
                env={**os.environ, "MCP_TESTER_FULL_RESPONSE": "1"})
            with anyio.fail_after(60):
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await session.call_tool(tool_name, arguments)

        try:
            result = anyio.run(call)
            error = None
        except Exception as err:  # the runtime crashed or timed out; its stderr says why
            result, error = None, err
        errlog.seek(0)
        stderr = errlog.read()
    return result, error, stderr


@handled
async def test_tool(request):
    form = await request.form()
    rel = form.get("file", "")
    path, data = open_project(rel)
    tool = form.get("tool", "")
    arguments = parse_arguments(form)

    result, error, stderr = await run_in_threadpool(run_tool, path, tool, arguments)
    return result_page(request, rel, data, tool, arguments, result, error, stderr)


def parse_arguments(form):
    try:
        arguments = json.loads(form.get("args", "") or "{}")
    except json.JSONDecodeError as err:
        raise UserError(f"Arguments aren't valid JSON: {err}")
    if not isinstance(arguments, dict):
        raise UserError('Arguments must be a JSON object, like {"q": "Boise,US"}.')
    return arguments


def field_paths(value, prefix="", depth=0, found=None):
    """Every dot path in a JSON value, walking through lists, up to 4 levels deep."""
    found = [] if found is None else found
    if isinstance(value, list):
        for item in value[:20]:
            field_paths(item, prefix, depth, found)
    elif isinstance(value, dict) and depth < 4:
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if path not in found:
                found.append(path)
            field_paths(child, path, depth + 1, found)
    return found


def result_page(request, rel, data, tool, arguments, result, error, stderr, where="local runtime"):
    if result is None:
        status = '<span class="tag bad">runtime failed</span>'
        output = f"The runtime didn't answer ({type(error).__name__}). Its error output is below."
        raw = ""
    else:
        status = '<span class="tag bad">tool error</span>' if result.is_error else '<span class="tag ok">ok</span>'
        full = "\n".join(getattr(c, "text", "") for c in result.content)
        cap = data.get("responseByteCap", 50_000)
        text = truncate(full, cap)  # what Claude actually gets
        size = f"Claude gets {len(text.encode('utf-8')):,} bytes (about {len(text) // 4:,} tokens)."
        if len(full.encode("utf-8")) > cap:
            size += (f'<div class="msg error">The full response is {len(full.encode("utf-8")):,} bytes, so it gets cut off '
                     f'at {cap:,}. Pick the fields you need on the project page so the useful part isn\'t lost.</div>')
        try:
            output = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            output = text
        try:
            paths = field_paths(json.loads(full))
        except (json.JSONDecodeError, TypeError):
            paths = []
        if paths:
            size += ("<div class=\"muted\">Fields in this response, for <i>Keep only these response fields</i>: "
                     + ", ".join(f"<code>{e(p)}</code>" for p in paths[:80])
                     + (f" and {len(paths) - 80} more" if len(paths) > 80 else "") + "</div>")
        raw = json.dumps(result.model_dump(by_alias=True, exclude_none=True), indent=2, ensure_ascii=False)

    body = f"""
<h1>{e(tool)}</h1><p class="sub">{e(data["name"])} · {e(where)} · {status} · <a href="/project?{urlencode({"file": rel})}">Back to project</a></p>
<h2>Arguments</h2><pre>{e(json.dumps(arguments, indent=2, ensure_ascii=False))}</pre>
<h2>Response</h2>{f'<div class="muted" style="margin-bottom:10px">{size}</div>' if result is not None else ''}<pre>{e(output)}</pre>
{f'<h2>Raw MCP result</h2><pre>{e(raw)}</pre>' if raw else ''}
{f'<h2>Runtime log (stderr)</h2><pre>{e(stderr)}</pre>' if stderr.strip() else ''}"""
    return page(f"Test {tool}", body, request)


# ----------------------------------------------------------------- install ---

def chosen_config(form):
    custom = form.get("custom_config", "").strip().strip('"')
    if custom:
        path = Path(custom).expanduser().resolve()
        if path.name != "claude_desktop_config.json" or not path.is_file():
            raise UserError("That path isn't an existing claude_desktop_config.json.")
        return path
    allowed = {str(c): c for c in installer.candidate_configs()}
    picked = form.get("config", "")
    if picked not in allowed:
        raise UserError("Pick a Claude Desktop config, or paste its path.")
    return allowed[picked]


def plan_change(action, config, name, project_path):
    servers = config.get("mcpServers", {})
    new_config = json.loads(json.dumps(config))  # deep copy
    notes = []
    if action == "add":
        entry = installer.build_entry(project_path)
        if name in servers and servers[name] != entry and not installer.is_ours(servers[name]):
            notes.append(f"This replaces an existing entry named '{name}' that this tool didn't create.")
        new_config.setdefault("mcpServers", {})[name] = entry
    else:
        if name not in servers:
            notes.append(f"'{name}' isn't in this config, so there's nothing to remove.")
        else:
            if not installer.is_ours(servers[name]):
                notes.append(f"The entry '{name}' wasn't created by this tool.")
            del new_config["mcpServers"][name]
    return new_config, notes


@handled
async def preview_change(request):
    form = await request.form()
    action = "remove" if form.get("action") == "remove" else "add"
    rel = form.get("file", "")
    path, data = open_project(rel)
    config_path = chosen_config(form)

    tools_html = ""
    if action == "add":
        await run_in_threadpool(installer.check_project, path)  # secrets present?
        tools = await run_in_threadpool(installer.verify_runtime, path)
        items = "".join(f"<li><b>{e(t.name)}</b>: {e(t.description or '')}</li>" for t in tools)
        tools_html = f'<div class="card"><b>Runtime started. Claude will get {len(tools)} tool(s):</b><ul>{items}</ul></div>'

    original_text, config = installer.load_config(config_path)
    new_config, notes = plan_change(action, config, data["name"], path)
    diff = installer.diff_text(config, new_config, config_path.name)

    warnings = [f'<div class="msg error">{e(n)}</div>' for n in notes]
    if installer.desktop_is_running():
        warnings.insert(0, '<div class="msg error">Claude Desktop is running. Quit it first '
                           '(tray icon, Quit), or it may overwrite this change.</div>')

    if new_config == config:
        confirm = '<p class="muted">Nothing to change.</p>'
    else:
        verb = "Install" if action == "add" else "Remove"
        confirm = f"""
<form method="post" action="/install/apply">
  <input type="hidden" name="file" value="{e(rel)}"><input type="hidden" name="action" value="{action}">
  <input type="hidden" name="custom_config" value="{e(str(config_path))}">
  <input type="hidden" name="digest" value="{text_hash(original_text)}">
  <button class="{"danger" if action == "remove" else ""}">{verb}: write this change</button>
  <span class="muted">The current file is backed up first.</span>
</form>"""

    title = "Install" if action == "add" else "Remove"
    body = f"""
<h1>{title} {e(data["name"])}</h1>
<p class="sub"><code>{e(str(config_path))}</code> · <a href="/project?{urlencode({"file": rel})}">Back</a></p>
{tools_html}{"".join(warnings)}
<h2>Change to the config file</h2><pre>{e(diff)}</pre>
{confirm}"""
    return page(title, body, request)


@handled
async def apply_change(request):
    form = await request.form()
    action = "remove" if form.get("action") == "remove" else "add"
    rel = form.get("file", "")
    path, data = open_project(rel)
    config_path = chosen_config(form)

    original_text, config = installer.load_config(config_path)
    if not same(text_hash(original_text), form.get("digest", "")):
        raise UserError("The config file changed after the preview (Claude Desktop may have saved it). "
                        "Nothing was written. Preview again.")
    new_config, _ = plan_change(action, config, data["name"], path)
    backup = installer.write_config(config_path, original_text, new_config)

    _, fresh = open_project(rel)
    if action == "add":
        record = {"client": installer.CLIENT_ID, "configPath": str(config_path), "serverName": data["name"],
                  "backupPath": str(backup), "status": "registered",
                  "registeredAt": datetime.now().isoformat(timespec="seconds")}
        msg = f"Installed. Restart Claude Desktop to load it. Backup: {backup}"
    else:
        record = {"configPath": str(config_path), "status": "removed"}
        msg = f"Removed. Restart Claude Desktop. Backup: {backup}"
    installer.record_registration(path, fresh, record)
    return go("/project", file=rel, msg=msg)


# ------------------------------------------------------------- claude code ---

def code_target(form):
    scope = form.get("scope", "user")
    folder = form.get("folder", "").strip().strip('"') or None
    cwd = claude_code.check_target(scope, folder)  # validates scope and folder
    return scope, (None if scope == "user" else str(cwd)), cwd


def remote_target(data):
    url = (data.get("deployment") or {}).get("url")
    if not url:
        raise UserError("Save the server URL first.")
    token = remote.get_token(data)
    if not token:
        raise UserError("Create a token first.")
    return url, token


@handled
async def code_preview(request):
    form = await request.form()
    action = "remove" if form.get("action") == "remove" else "add"
    is_remote = form.get("remote") == "1"
    rel = form.get("file", "")
    path, data = open_project(rel)
    scope, folder, cwd = code_target(form)
    name = claude_code.remote_name(data["name"]) if is_remote else data["name"]

    tools_html = ""
    if action == "add":
        if is_remote:
            url, token = remote_target(data)
            tools, _ = await run_in_threadpool(remote.remote_session, url, token)
            heading = f"Remote server answered. Claude will get {len(tools)} tool(s):"
        else:
            await run_in_threadpool(installer.check_project, path)
            tools = await run_in_threadpool(installer.verify_runtime, path)
            heading = f"Runtime started. Claude will get {len(tools)} tool(s):"
        items = "".join(f"<li><b>{e(t.name)}</b>: {e(t.description or '')}</li>" for t in tools)
        tools_html = f'<div class="card"><b>{heading}</b><ul>{items}</ul></div>'

    existing = await run_in_threadpool(claude_code.get, name, cwd)
    notes = []
    if action == "add" and not is_remote:
        notes = [f'<div class="msg error">{e(w)}</div>' for w in claude_code.warnings_for(scope)]
    if action == "add" and is_remote:
        notes.append('<div class="msg info">Claude Code keeps the token in its own config file, in plain text.</div>')
    replace_box = ""
    if existing:
        mine = (data.get("deployment", {}).get("url", "") in existing) if is_remote else claude_code.is_ours(existing)
        notes.append(f'<div class="card"><b>Claude Code already has a server named {e(name)}'
                     f'{"" if mine else ", not created by this tool"}:</b><pre>{e(existing)}</pre></div>')
        if action == "add" and not mine:
            replace_box = ('<label><input type="checkbox" name="replace" value="1"> '
                           f'Replace it if it\'s in the {e(scope)} scope</label>')
    elif action == "remove":
        notes.append(f'<div class="msg error">Claude Code has no server named {e(name)} here. '
                     "Nothing to remove.</div>")

    if action == "add":
        args = (claude_code.add_http_args(name, *remote_target(data), scope) if is_remote
                else claude_code.add_args(name, path, scope))
    else:
        args = claude_code.remove_args(name, scope)
    can_run = action == "add" or existing
    confirm = f"""
<form method="post" action="/code/apply">
  <input type="hidden" name="file" value="{e(rel)}"><input type="hidden" name="action" value="{action}">
  <input type="hidden" name="scope" value="{e(scope)}"><input type="hidden" name="folder" value="{e(folder or "")}">
  <input type="hidden" name="remote" value="{"1" if is_remote else ""}">
  {replace_box}
  <button class="{"danger" if action == "remove" else ""}">{"Install" if action == "add" else "Remove"}: run this command</button>
</form>""" if can_run else ""

    where = "every project" if scope == "user" else f"<code>{e(folder)}</code>"
    body = f"""
<h1>{"Install" if action == "add" else "Remove"} {e(name)} in Claude Code</h1>
<p class="sub">{e(scope)} scope · {where} · <a href="/project?{urlencode({"file": rel})}">Back</a></p>
{tools_html}{"".join(notes)}
<h2>Command</h2><pre>{e(claude_code.display(args))}</pre>
{confirm}"""
    return page("Claude Code", body, request)


@handled
async def code_apply(request):
    form = await request.form()
    action = "remove" if form.get("action") == "remove" else "add"
    is_remote = form.get("remote") == "1"
    rel = form.get("file", "")
    path, data = open_project(rel)
    scope, folder, cwd = code_target(form)
    name = claude_code.remote_name(data["name"]) if is_remote else data["name"]
    key = claude_code.registration_key(scope, folder, remote=is_remote)

    if action == "add":
        if is_remote:
            url, token = remote_target(data)
            output = await run_in_threadpool(claude_code.add, name, path, scope, folder,
                                             form.get("replace") == "1", url, token)
        else:
            await run_in_threadpool(installer.check_project, path)
            output = await run_in_threadpool(claude_code.add, name, path, scope, folder, form.get("replace") == "1")
        record = {"client": claude_code.CLIENT_ID, "configPath": key, "scope": scope, "folder": folder,
                  "serverName": name, "status": "registered",
                  "registeredAt": datetime.now().isoformat(timespec="seconds")}
        if is_remote:
            record.update(remote=True, url=data["deployment"]["url"])
        msg = "Registered with Claude Code. Start a new session and run /mcp to check it."
        if scope == "project":
            msg += " Claude Code will ask you to approve it."
    else:
        output = await run_in_threadpool(claude_code.remove, name, scope, folder)
        record = {"client": claude_code.CLIENT_ID, "configPath": key, "status": "removed"}
        msg = "Removed from Claude Code."
    _, fresh = open_project(rel)
    installer.record_registration(path, fresh, record)
    last_line = output.strip().splitlines()[0] if output.strip() else ""
    return go("/project", file=rel, msg=f"{msg} ({last_line})" if last_line else msg)


# ------------------------------------------------------------------ remote ---

@handled
async def remote_token(request):
    form = await request.form()
    rel = form.get("file", "")
    _, data = open_project(rel)
    action = form.get("action")
    existing = remote.get_token(data)

    if action == "show":
        if not existing:
            raise UserError("There's no token yet.")
        token, heading = existing, "Your token"
        note = "This is the token already in use."
    elif action == "create" and existing:
        raise UserError("A token already exists. Use Show token, or Replace token.")
    elif action in ("create", "replace"):
        token = await run_in_threadpool(remote.create_token, data)
        heading = "New token"
        note = ("Stored in your OS keychain." + (" The old token stops working once you update the host "
                "and every client." if action == "replace" else ""))
    else:
        raise UserError("Unknown action.")

    body = f"""
<h1>{heading}</h1><p class="sub">{e(data["name"])} · <a href="/project?{urlencode({"file": rel})}">Back to project</a></p>
<pre>{e(token)}</pre>
<p>{e(note)}</p>
<ul>
  <li>On the host: set <code>{e(remote.BEARER_ENV)}</code> to this value.</li>
  <li>On your server's sign-in page (when claude.ai connects with Sign in now): paste this value and click Allow.</li>
  <li>In claude.ai's Add custom connector: Authentication <b>No sign-in</b>, then Request headers <code>x-api-key</code> = this value.</li>
  <li>For Claude Code and other clients: header <code>Authorization</code> = <code>Bearer</code> followed by this value.</li>
</ul>
<p class="muted">Anyone with this token can call your tools, and spend your API quota. Don't paste it into chats,
screenshots or git.</p>"""
    return page(heading, body, request)


@handled
async def remote_url(request):
    form = await request.form()
    rel = form.get("file", "")
    path, data = open_project(rel)
    url = remote.validate_url(form.get("url", ""))
    data["deployment"] = {**(data.get("deployment") or {}), "mode": "remote", "url": url}
    installer.atomic_write(path, installer.to_text(data))
    return go("/project", file=rel, msg="Server URL saved. Test it below.")


@handled
async def remote_test(request):
    form = await request.form()
    rel = form.get("file", "")
    _, data = open_project(rel)
    url, token = remote_target(data)
    tool = form.get("tool", "")
    arguments = parse_arguments(form)
    try:
        _, result = await run_in_threadpool(remote.remote_session, url, token, tool, arguments)
        error = None
    except installer.InstallError as err:
        result, error = None, err
    if result is None:
        raise UserError(str(error))
    return result_page(request, rel, data, tool, arguments, result, None, "", where=url)


# --------------------------------------------------------------------- app ---

@handled
async def show_draft(request):
    return await render_draft(request)


app = Starlette(
    routes=[
        Route("/", home),
        Route("/import", start_import, methods=["POST"]),
        Route("/draft/{draft_id}", show_draft, methods=["GET"]),
        Route("/draft/{draft_id}", save_draft, methods=["POST"]),
        Route("/project", show_project),
        Route("/project/save", save_project, methods=["POST"]),
        Route("/secret", store_secret, methods=["POST"]),
        Route("/registry", registry_search),
        Route("/registry/server", registry_server),
        Route("/registry/preview", registry_preview, methods=["POST"]),
        Route("/registry/apply", registry_apply, methods=["POST"]),
        Route("/registry/remove", registry_remove, methods=["POST"]),
        Route("/oauth/client", oauth_client, methods=["POST"]),
        Route("/oauth/start", oauth_start, methods=["POST"]),
        Route("/oauth/logout", oauth_logout, methods=["POST"]),
        Route("/test", test_tool, methods=["POST"]),
        Route("/install/preview", preview_change, methods=["POST"]),
        Route("/install/apply", apply_change, methods=["POST"]),
        Route("/code/preview", code_preview, methods=["POST"]),
        Route("/code/apply", code_apply, methods=["POST"]),
        Route("/remote/token", remote_token, methods=["POST"]),
        Route("/remote/url", remote_url, methods=["POST"]),
        Route("/remote/test", remote_test, methods=["POST"]),
    ],
    middleware=[Middleware(BaseHTTPMiddleware, dispatch=guard)],
)


def main():
    parser = argparse.ArgumentParser(description="Browser UI for mcp-setup-gui")
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    opts = parser.parse_args()

    url = f"http://127.0.0.1:{PORT}/?t={TOKEN}"
    print(f"MCP Setup is running. Open:\n  {url}\nPress Ctrl+C to stop.", flush=True)
    if not opts.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
