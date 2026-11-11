"""Find MCP servers in the official MCP Registry and install them in Claude.

    uv run registry.py search github
    uv run registry.py show io.github.someone/server
    uv run registry.py install io.github.someone/server [--client claude-code] [--option 2] [--name NAME]
    uv run registry.py list
    uv run registry.py remove NAME

The registry (registry.modelcontextprotocol.io) lists servers as server.json
entries: packages to run locally (npm, PyPI, Docker) and remote URLs. Install
turns one of those into a client registration:

  - A package runs through launch.py, which fills in secrets from the OS
    keychain at start, so API keys never sit in a client's config file.
  - A remote server is registered with Claude Code by URL. Claude Desktop
    and claude.ai add remote servers as custom connectors instead.

The registry only stores metadata. It doesn't review the code a package runs,
so check who publishes a server before you install it.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

import install as installer
from install import InstallError

HERE = Path(__file__).resolve().parent
LAUNCH = HERE / "launch.py"
REGISTRY_URL = os.environ.get("MCP_REGISTRY_URL", "https://registry.modelcontextprotocol.io").rstrip("/")
SPEC_DIR = Path.home() / ".mcpgui" / "servers"
KEYCHAIN_SERVICE = "mcp-setup-gui"
META = "io.modelcontextprotocol.registry/official"

# How each package type runs: (command, what to install if it's missing)
RUNNERS = {
    "npm": ("npx", "Node.js (npx)"),
    "pypi": ("uvx", "uv (uvx)"),
    "oci": ("docker", "Docker"),
    "nuget": ("dnx", ".NET 10 SDK (dnx)"),
}


class RegistryError(InstallError):
    pass


# ------------------------------------------------------------------ fetch ---

def get_json(path, params=None):
    try:
        response = httpx.get(REGISTRY_URL + path, params=params, timeout=20,
                             headers={"accept": "application/json"})
    except httpx.RequestError as e:
        raise RegistryError(f"Couldn't reach the MCP Registry ({type(e).__name__}).") from None
    if response.status_code == 404:
        raise RegistryError("The registry has no server by that name.")
    if response.status_code >= 400:
        raise RegistryError(f"The registry returned {response.status_code}.")
    try:
        return response.json()
    except ValueError:
        raise RegistryError("The registry sent something that isn't JSON.") from None


def search(query, limit=30, cursor=None):
    """(entries, next cursor). Each entry: {"server": server.json, "_meta": {...}}."""
    params = {"search": query, "version": "latest", "limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = get_json("/v0.1/servers", params)
    entries = [e for e in data.get("servers") or [] if isinstance(e.get("server"), dict)]
    return entries, (data.get("metadata") or {}).get("nextCursor") or None


def fetch(name, version="latest"):
    if not re.fullmatch(r"[A-Za-z0-9.\-]+/[A-Za-z0-9._\-]+", name or ""):
        raise RegistryError(f"{name!r} isn't a registry server name (they look like io.github.someone/server).")
    data = get_json(f"/v0.1/servers/{quote(name, safe='')}/versions/{quote(version, safe='')}")
    if not isinstance(data.get("server"), dict):
        raise RegistryError("The registry's answer had no server in it.")
    return data


def status(entry):
    return ((entry.get("_meta") or {}).get(META) or {}).get("status", "active")


# ----------------------------------------------------------------- inputs ---
# server.json describes what a user must fill in: environment variables,
# arguments, headers and URL variables. Any of them can be a template like
# "Bearer {token}" whose {token} is its own input.

TEMPLATE_VAR = re.compile(r"\{([A-Za-z0-9_\-]+)\}")


def field(key, label, spec, owner_secret=False):
    choices = spec.get("choices") or []
    return {
        "key": key,
        "label": label,
        "description": spec.get("description", ""),
        "required": bool(spec.get("isRequired")),
        "secret": bool(spec.get("isSecret") or owner_secret),
        "default": spec.get("default", "") or "",
        "choices": [str(c) for c in choices],
        "placeholder": spec.get("placeholder", "") or "",
    }


def item_fields(key, label, item):
    """The inputs one env var / argument / header needs. None means it has a fixed value."""
    value = item.get("value")
    variables = item.get("variables") or {}
    if value:
        names = TEMPLATE_VAR.findall(value)
        return [field(f"{key}:{v}", f"{label} → {v}", variables.get(v, {}), item.get("isSecret"))
                for v in names if v in variables]
    return [field(key, label, item)]


def item_value(key, item, values):
    """The final string for one item, or None if it was optional and left empty."""
    value = item.get("value")
    variables = item.get("variables") or {}
    if value:
        def fill(m):
            v = m.group(1)
            if v not in variables:
                return m.group(0)
            got = values.get(f"{key}:{v}") or variables[v].get("default", "")
            if not got and variables[v].get("isRequired"):
                raise RegistryError(f"{v} is required.")
            return got
        return TEMPLATE_VAR.sub(fill, value)
    got = values.get(key, "") or item.get("default", "") or ""
    if not got:
        if item.get("isRequired"):
            raise RegistryError(f"{item.get('name') or key} is required.")
        return None
    return got


def is_secret(item):
    return bool(item.get("isSecret") or any(v.get("isSecret") for v in (item.get("variables") or {}).values()))


def arg_label(arg, i):
    return arg.get("name") or arg.get("valueHint") or f"argument {i + 1}"


# ---------------------------------------------------------------- options ---

def options(server):
    """Ways to install this server, in the order a user should prefer them."""
    found = []
    for i, pkg in enumerate(server.get("packages") or []):
        kind = pkg.get("registryType", "")
        transport = (pkg.get("transport") or {}).get("type", "stdio")
        opt = {"id": f"p{i}", "kind": "package", "package": pkg, "registryType": kind,
               "label": f"{kind} package {pkg.get('identifier', '?')} {pkg.get('version', '')}".strip()}
        if kind == "mcpb":
            opt["unsupported"] = "an MCPB bundle: download it and open it in Claude Desktop"
        elif kind not in RUNNERS:
            opt["unsupported"] = f"{kind or 'unknown'} packages aren't supported yet"
        elif transport != "stdio":
            opt["unsupported"] = f"this package runs its own {transport} server, which isn't supported yet"
        else:
            command, needs = RUNNERS[kind]
            opt.update(command=command, needs=needs, found=shutil.which(command))
            opt["fields"] = package_fields(pkg)
        found.append(opt)
    for i, remote in enumerate(server.get("remotes") or []):
        kind = remote.get("type", "")
        opt = {"id": f"r{i}", "kind": "remote", "remote": remote,
               "label": f"remote server ({kind}) {remote.get('url', '')}"}
        if kind not in ("streamable-http", "sse"):
            opt["unsupported"] = f"{kind or 'unknown'} transport"
        else:
            opt["fields"] = remote_fields(remote)
        found.append(opt)
    return found


def package_fields(pkg):
    fields = []
    for i, arg in enumerate(pkg.get("runtimeArguments") or []):
        fields += item_fields(f"rarg:{i}", arg_label(arg, i), arg)
    for i, arg in enumerate(pkg.get("packageArguments") or []):
        fields += item_fields(f"arg:{i}", arg_label(arg, i), arg)
    for env in pkg.get("environmentVariables") or []:
        fields += item_fields(f"env:{env['name']}", env["name"], env)
    return fields


def remote_fields(remote):
    fields = []
    url_vars = remote.get("variables") or {}
    for v in TEMPLATE_VAR.findall(remote.get("url", "")):
        if v in url_vars:
            fields.append(field(f"url:{v}", f"URL → {v}", url_vars[v]))
    for header in remote.get("headers") or []:
        fields += item_fields(f"header:{header['name']}", header["name"], header)
    return fields


def find_option(server, option_id):
    opts = options(server)
    if option_id is None:
        usable = [o for o in opts if "unsupported" not in o]
        if not usable:
            reasons = "; ".join(f"{o['label']}: {o['unsupported']}" for o in opts) or "it lists no packages or URLs"
            raise RegistryError(f"No way to install this server here ({reasons}).")
        return usable[0]
    opt = next((o for o in opts if o["id"] == option_id), None)
    if not opt:
        raise RegistryError(f"No install option {option_id!r}. Options: {', '.join(o['id'] for o in opts)}")
    if "unsupported" in opt:
        raise RegistryError(f"That option can't be installed here: {opt['unsupported']}.")
    return opt


# ------------------------------------------------------------------- plan ---

def server_slug(name):
    """io.github.someone/weather-server -> weather-server"""
    return re.sub(r"[^A-Za-z0-9_\-]", "-", name.rsplit("/", 1)[-1]).strip("-")[:40] or "server"


def check_local_name(local):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", local or ""):
        raise RegistryError("The name in Claude must be letters, numbers, - or _ (up to 64).")
    return local


def secret_ref(local, what):
    return f"secret://registry/{local}/{re.sub(r'[^A-Za-z0-9_.-]', '_', what)}"


def render_args(items, prefix, values, local, secrets):
    """Arguments as a list of strings and {"secretRef"} placeholders."""
    out = []
    for i, arg in enumerate(items or []):
        key = f"{prefix}:{i}"
        value = item_value(key, arg, values)
        named = arg.get("type") == "named"
        if value is None:
            continue  # optional and left empty
        if is_secret(arg):
            ref = secret_ref(local, f"{prefix}{i}")
            secrets[ref] = value
            value = {"secretRef": ref}
        out += [arg["name"], value] if named and arg.get("name") else [value]
    return out


def image_ref(pkg):
    ident = pkg["identifier"]
    last = ident.rsplit("/", 1)[-1]
    if ":" in last or "@" in ident or not pkg.get("version"):
        return ident
    return f"{ident}:{pkg['version']}"


def plan_package(entry, opt, values, local):
    server, pkg = entry["server"], opt["package"]
    kind = opt["registryType"]
    secrets, env, secret_env = {}, {}, {}
    for item in pkg.get("environmentVariables") or []:
        value = item_value(f"env:{item['name']}", item, values)
        if value is None:
            continue
        if is_secret(item):
            ref = secret_ref(local, item["name"])
            secrets[ref] = value
            secret_env[item["name"]] = ref
        else:
            env[item["name"]] = value

    runtime_args = render_args(pkg.get("runtimeArguments"), "rarg", values, local, secrets)
    package_args = render_args(pkg.get("packageArguments"), "arg", values, local, secrets)
    ident, version = pkg["identifier"], pkg.get("version")
    if kind == "npm":
        args = [*runtime_args, "-y", f"{ident}@{version}" if version else ident, *package_args]
    elif kind == "pypi":
        args = [*runtime_args, f"{ident}=={version}" if version else ident, *package_args]
    elif kind == "oci":
        # A container doesn't see the launcher's environment: pass each variable by name
        passthrough = [x for name in [*env, *secret_env] for x in ("-e", name)]
        args = ["run", "-i", "--rm", *runtime_args, *passthrough, image_ref(pkg), *package_args]
    else:  # nuget
        args = [*runtime_args, f"{ident}@{version}" if version else ident, "--yes", *package_args]

    spec = {
        "registryName": server["name"],
        "version": server.get("version", ""),
        "option": opt["label"],
        "command": opt.get("found") or opt["command"],
        "needs": opt["needs"],
        "args": args,
        "env": env,
        "secretEnv": secret_env,
    }
    return spec, secrets


def plan_remote(entry, opt, values):
    remote = opt["remote"]
    url_vars = remote.get("variables") or {}

    def fill(m):
        v = m.group(1)
        if v not in url_vars:
            return m.group(0)
        got = values.get(f"url:{v}") or url_vars[v].get("default", "")
        if not got:
            raise RegistryError(f"URL value {v} is required.")
        return quote(got, safe="")
    url = TEMPLATE_VAR.sub(fill, remote["url"])
    if not url.startswith("https://") and not re.match(r"http://(127\.0\.0\.1|localhost)[:/]", url):
        raise RegistryError("Remote servers must use https://.")
    headers = {}
    for header in remote.get("headers") or []:
        value = item_value(f"header:{header['name']}", header, values)
        if value is not None:
            headers[header["name"]] = value
    return {"registryName": entry["server"]["name"], "version": entry["server"].get("version", ""),
            "option": opt["label"], "remote": {"type": remote["type"], "url": url}}, headers


def spec_path(local):
    return SPEC_DIR / f"{local}.json"


def desktop_entry(local):
    return {"command": sys.executable, "args": [str(LAUNCH), "--spec", str(spec_path(local))]}


def is_ours(entry):
    return any(str(a).endswith("launch.py") for a in entry.get("args", []))


def installed():
    found = []
    if SPEC_DIR.is_dir():
        for path in sorted(SPEC_DIR.glob("*.json")):
            try:
                found.append((path.stem, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    return found


# ----------------------------------------------------------------- install ---

def save_spec(local, spec, secrets):
    import keyring
    for ref, value in secrets.items():
        keyring.set_password(KEYCHAIN_SERVICE, ref.removeprefix("secret://"), value)
    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    spec["installedAt"] = datetime.now().isoformat(timespec="seconds")
    try:  # installing in a second client keeps the record of the first
        spec["clients"] = json.loads(spec_path(local).read_text(encoding="utf-8")).get("clients", [])
    except (OSError, ValueError):
        pass
    installer.atomic_write(spec_path(local), installer.to_text(spec))


def forget_secrets(spec):
    import keyring
    refs = list(spec.get("secretEnv", {}).values())
    refs += [a["secretRef"] for a in spec.get("args", []) if isinstance(a, dict)]
    for ref in refs:
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, ref.removeprefix("secret://"))
        except Exception:
            pass


def desktop_change(local, config_path, replace_foreign=False):
    """(original text, current config, new config) for adding this server to Claude Desktop."""
    original_text, config = installer.load_config(config_path)
    current = (config.get("mcpServers") or {}).get(local)
    if current and not is_ours(current) and not replace_foreign:
        raise RegistryError(f"Claude Desktop already has a server named '{local}' that MCP Setup didn't add. "
                            "Pick another name.")
    new_config = json.loads(json.dumps(config))
    new_config.setdefault("mcpServers", {})[local] = desktop_entry(local)
    return original_text, config, new_config


def code_args(local, spec, headers, scope):
    if "remote" in spec:
        transport = "http" if spec["remote"]["type"] == "streamable-http" else "sse"
        args = ["mcp", "add", "--transport", transport, "--scope", scope, local, spec["remote"]["url"]]
        for name, value in headers.items():
            args += ["--header", f"{name}: {value}"]
        return args
    return ["mcp", "add", "--transport", "stdio", "--scope", scope, local, "--",
            sys.executable, str(LAUNCH), "--spec", str(spec_path(local))]


def record_client(local, record):
    path = spec_path(local)
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["clients"] = [c for c in spec.get("clients", []) if c != record] + [record]
    installer.atomic_write(path, installer.to_text(spec))


def install_desktop(local, spec, secrets, config_path, original_text, new_config):
    if "remote" in spec:
        raise RegistryError("Claude Desktop adds remote servers as connectors: Settings → Connectors → "
                            f"Add custom connector, URL {spec['remote']['url']}")
    save_spec(local, spec, secrets)
    try:
        backup = installer.write_config(config_path, original_text, new_config)
    except Exception:
        if not spec_registered_elsewhere(local):
            forget_secrets(spec)
            spec_path(local).unlink(missing_ok=True)
        raise
    record_client(local, {"client": "claude-desktop", "configPath": str(config_path)})
    return backup


def install_code(local, spec, secrets, headers, scope, folder):
    import claude_code
    cwd = claude_code.check_target(scope, folder)
    save_spec(local, spec, secrets)
    code, output = claude_code.run(code_args(local, spec, headers, scope), cwd)
    if code != 0:
        if "already exists" in output:
            output = (f"Claude Code already has a server named '{local}' in {scope} scope. "
                      "Remove it first, or pick another name.")
        if not spec_registered_elsewhere(local):
            forget_secrets(spec)
            spec_path(local).unlink(missing_ok=True)
        raise RegistryError(f"claude mcp add failed: {claude_code.mask(output)}")
    record_client(local, {"client": "claude-code", "scope": scope,
                          "folder": str(cwd) if scope != "user" else None})
    return claude_code.mask(output)


def spec_registered_elsewhere(local):
    try:
        return bool(json.loads(spec_path(local).read_text(encoding="utf-8")).get("clients"))
    except (OSError, ValueError):
        return False


def remove(local, config_hint=None):
    """Unregister from every client it was installed in, then delete the spec and secrets."""
    import claude_code
    path = spec_path(local)
    if not path.is_file():
        raise RegistryError(f"No server called '{local}' was installed from the registry.")
    spec = json.loads(path.read_text(encoding="utf-8"))
    notes = []
    for client in spec.get("clients", []):
        if client["client"] == "claude-desktop":
            config_path = Path(client["configPath"])
            if not config_path.is_file():
                continue
            original_text, config = installer.load_config(config_path)
            current = (config.get("mcpServers") or {}).get(local)
            if current and is_ours(current):
                new_config = json.loads(json.dumps(config))
                del new_config["mcpServers"][local]
                backup = installer.write_config(config_path, original_text, new_config)
                notes.append(f"Removed from Claude Desktop (backup {backup}). Restart Desktop.")
        elif client["client"] == "claude-code":
            try:
                claude_code.remove(local, client["scope"], client.get("folder"))
                notes.append(f"Removed from Claude Code ({client['scope']} scope).")
            except InstallError as e:
                notes.append(f"Claude Code: {e}")
    forget_secrets(spec)
    path.unlink()
    return notes


# --------------------------------------------------------------------- cli ---

def short(text, n=90):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def prompt_values(fields):
    values = {}
    for f in fields:
        hint = f" ({f['description']})" if f["description"] else ""
        default = f" [{f['default']}]" if f["default"] and not f["secret"] else ""
        choices = f" one of: {', '.join(f['choices'])}" if f["choices"] else ""
        label = f"{f['label']}{' (required)' if f['required'] else ''}{hint}{choices}{default}: "
        while True:
            value = (getpass.getpass(label) if f["secret"] else input(label)).strip()
            if value or f["default"] or not f["required"]:
                break
            print("  Required.")
        values[f["key"]] = value
    return values


def cmd_search(opts):
    entries, _ = search(opts.query, limit=opts.limit)
    if not entries:
        print("No servers found.")
    for e in entries:
        s = e["server"]
        kinds = sorted({p.get("registryType", "?") for p in s.get("packages") or []}
                       | ({"remote"} if s.get("remotes") else set()))
        flag = "" if status(e) == "active" else f" [{status(e)}]"
        print(f"{s['name']}  {s.get('version', '')}  ({', '.join(kinds) or 'nothing to install'}){flag}")
        print(f"    {short(s.get('description'))}")


def resolve_name(name):
    """Accept a short name like "time": search, and use it if exactly one server ends with it."""
    if "/" in name:
        return name
    entries, _ = search(name)
    exact = [e["server"]["name"] for e in entries if e["server"]["name"].rsplit("/", 1)[-1].lower() == name.lower()]
    if len(exact) == 1:
        return exact[0]
    names = exact or [e["server"]["name"] for e in entries]
    if not names:
        raise RegistryError(f"No server named like {name!r}. Try: uv run registry.py search {name}")
    raise RegistryError(f"{name!r} matches several servers. Use the full name:\n  " + "\n  ".join(names[:15]))


def cmd_show(opts):
    entry = fetch(resolve_name(opts.name))
    s = entry["server"]
    print(f"{s.get('title') or s['name']}  {s.get('version', '')}  [{status(entry)}]")
    print(short(s.get("description"), 400))
    if (s.get("repository") or {}).get("url"):
        print(f"Source: {s['repository']['url']}")
    print("\nInstall options:")
    for o in options(s):
        if "unsupported" in o:
            print(f"  {o['id']}: {o['label']}  (not here: {o['unsupported']})")
            continue
        extra = ""
        if o["kind"] == "package":
            extra = "" if o["found"] else f"  (needs {o['needs']}, not found)"
        print(f"  {o['id']}: {o['label']}{extra}")
        for f in o["fields"]:
            print(f"        {f['label']}{' *' if f['required'] else ''}{' (secret)' if f['secret'] else ''}"
                  f"{': ' + short(f['description'], 70) if f['description'] else ''}")


def cmd_install(opts):
    entry = fetch(resolve_name(opts.name))
    opt = find_option(entry["server"], opts.option)
    local = check_local_name(opts.local_name or server_slug(entry["server"]["name"]))
    print(f"Installing {entry['server']['name']} {entry['server'].get('version', '')} as '{local}' "
          f"using {opt['label']}")
    if opt["kind"] == "package" and not opt["found"]:
        print(f"Warning: {opt['needs']} wasn't found on PATH. Install it before starting Claude.")
    if opt["kind"] == "remote" and opts.client == "claude-desktop":
        raise RegistryError("Claude Desktop adds remote servers as connectors: Settings → Connectors → "
                            f"Add custom connector, URL {opt['remote']['url']}. Or install it in Claude Code "
                            "with --client claude-code.")
    values = prompt_values(opt["fields"])

    if opt["kind"] == "remote":
        spec, headers = plan_remote(entry, opt, values)
        secrets = {}
    else:
        spec, secrets = plan_package(entry, opt, values, local)
        headers = {}

    if opts.client == "claude-code":
        print(install_code(local, spec, secrets, headers, opts.scope, opts.folder))
        if headers:
            print("Note: Claude Code keeps these headers in its own config file, in plain text.")
        return
    if "remote" in spec:
        raise RegistryError("Claude Desktop adds remote servers as connectors: Settings → Connectors → "
                            f"Add custom connector, URL {spec['remote']['url']}")
    config_path = installer.find_config(opts.config, opts.yes)
    installer.warn_if_running(opts.yes)
    original_text, config, new_config = desktop_change(local, config_path)
    installer.show_diff(config, new_config, config_path.name)
    if not installer.confirm("\nWrite this change?", opts.yes):
        raise RegistryError("Cancelled. Nothing was written.")
    backup = install_desktop(local, spec, secrets, config_path, original_text, new_config)
    print(f"Installed '{local}'. Backup of the old config: {backup}")
    installer.restart_hint()


def cmd_list(opts):
    rows = installed()
    if not rows:
        print("Nothing installed from the registry yet.")
    for local, spec in rows:
        where = ", ".join(c["client"] + (f" ({c['scope']})" if c.get("scope") else "")
                          for c in spec.get("clients", [])) or "no client"
        print(f"{local}: {spec.get('registryName')} {spec.get('version', '')} in {where}")


def cmd_remove(opts):
    for note in remove(check_local_name(opts.local_name)):
        print(note)
    print(f"'{opts.local_name}' removed, and its secrets deleted from the keychain.")


def main():
    parser = argparse.ArgumentParser(description="Install servers from the MCP Registry")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("search", help="Search servers by name")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("show", help="Show a server's install options")
    p.add_argument("name")
    p = sub.add_parser("install", help="Install a server in Claude Desktop or Claude Code")
    p.add_argument("name")
    p.add_argument("--option", help="Install option id from `show` (default: the first that works here)")
    p.add_argument("--name", dest="local_name", help="Name in Claude (default: the last part of the registry name)")
    p.add_argument("--client", choices=["claude-desktop", "claude-code"], default="claude-desktop")
    p.add_argument("--scope", default="user", help="Claude Code scope: user, local or project")
    p.add_argument("--folder", help="Claude Code project folder, for local and project scope")
    p.add_argument("--config", help="Claude Desktop config file")
    p.add_argument("--yes", action="store_true")
    sub.add_parser("list", help="Servers installed from the registry")
    p = sub.add_parser("remove", help="Uninstall a server installed from the registry")
    p.add_argument("local_name")
    opts = parser.parse_args()
    try:
        {"search": cmd_search, "show": cmd_show, "install": cmd_install,
         "list": cmd_list, "remove": cmd_remove}[opts.command](opts)
    except InstallError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")


if __name__ == "__main__":
    main()
