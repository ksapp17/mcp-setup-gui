"""Installer: registers a project's runtime with Claude Desktop or Claude Code.

Usage (from the project folder):
    uv run install.py add     --project project.json
    uv run install.py remove  --project project.json
    uv run install.py list

    uv run install.py add    --project project.json --client claude-code [--scope user|local|project] [--folder DIR]
    uv run install.py remove --project project.json --client claude-code
    uv run install.py add    --project project.json --client claude-code --url https://host/mcp   (remote server)
    uv run install.py remove --project project.json --client claude-code --remote

Options:
    --client NAME   claude-desktop (default) or claude-code
    --config PATH   Claude Desktop: use this claude_desktop_config.json instead of searching
    --scope SCOPE   Claude Code: user (default, every project), local or project (one folder)
    --folder DIR    Claude Code: the project folder, for local and project scope
    --yes           don't ask for confirmation
    --skip-verify   don't start the runtime to check it before installing

Claude Code registrations go through Claude Code's own `claude mcp add` and
`claude mcp remove` (see claude_code.py). What "add" does for Claude Desktop, in order:
  1. Checks project.json and that every secret it needs is in the keychain
  2. Starts the runtime and lists its tools, so you see what Claude will get
  3. Finds Claude Desktop's config file (it isn't always where the docs say)
  4. Shows a diff of the change and asks before writing
  5. Backs up the config, writes a temp file, swaps it in, re-reads it to confirm
  6. Records the registration in project.json so "remove" can undo it cleanly
"""

import argparse
import difflib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime.py"
BACKUP_DIR = Path.home() / ".mcpgui" / "backups"
KEYCHAIN_SERVICE = "mcp-setup-gui"
CLIENT_ID = "claude-desktop"


class InstallError(Exception):
    pass


# ------------------------------------------------------------------ helpers ---

def confirm(question, assume_yes):
    if assume_yes:
        return True
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def to_text(data):
    # indent=2 matches how Claude Desktop writes the file, so diffs stay small
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def atomic_write(path, text):
    """Write to a temp file in the same folder, then swap it in.
    A crash halfway leaves the original file untouched."""
    path = Path(path)
    tmp = path.with_name(path.name + ".mcpgui-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def backup(path):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"{Path(path).stem}.{stamp}.json"
    n = 1
    while dest.exists():  # two writes in the same second must not share a backup
        dest = BACKUP_DIR / f"{Path(path).stem}.{stamp}-{n}.json"
        n += 1
    shutil.copy2(path, dest)
    return dest


def desktop_is_running():
    try:
        system = platform.system()
        if system == "Windows":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq claude.exe", "/NH"],
                                 capture_output=True, text=True).stdout
            return "claude.exe" in out.lower()
        if system == "Darwin":
            return subprocess.run(["pgrep", "-x", "Claude"], capture_output=True).returncode == 0
    except OSError:
        pass
    return False


# ------------------------------------------------------- find the config file ---

def candidate_configs():
    """Every place Claude Desktop has been seen keeping its config."""
    home = Path.home()
    system = platform.system()
    paths = []
    if system == "Windows":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        paths.append(appdata / "Claude" / "claude_desktop_config.json")
        paths += sorted((local / "Claude-Data").glob("claude_*/claude_desktop_config.json"))
        paths += sorted((local / "Packages").glob(
            "Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json"))
    elif system == "Darwin":
        paths.append(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    else:
        paths.append(home / ".config" / "Claude" / "claude_desktop_config.json")
    return [p for p in paths if p.is_file()]


def find_config(explicit, assume_yes):
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise InstallError(f"No config file at {path}")
        return path

    found = candidate_configs()
    if not found:
        raise InstallError(
            "Couldn't find claude_desktop_config.json.\n"
            "Open Claude Desktop > Settings > Desktop app > Extensions > Edit config "
            "to create it, then run this again (or pass --config PATH).")
    if len(found) == 1:
        return found[0]
    if assume_yes:
        raise InstallError("Found several config files; pass --config to pick one:\n  "
                           + "\n  ".join(map(str, found)))
    print("Found more than one Claude Desktop config:")
    for i, p in enumerate(found, 1):
        print(f"  {i}. {p}")
    choice = input("Which one does Desktop use? (Settings > Edit config opens it) Number: ")
    try:
        return found[int(choice) - 1]
    except (ValueError, IndexError):
        raise InstallError("No config chosen")


# ------------------------------------------------------------ check project ---

def check_project(project_path):
    if not project_path.is_file():
        raise InstallError(f"No project file at {project_path}")
    try:
        project = read_json(project_path)
    except json.JSONDecodeError as e:
        raise InstallError(f"project.json is not valid JSON: {e}")

    for key in ("name", "apis"):
        if key not in project:
            raise InstallError(f"project.json is missing '{key}'")

    import keyring
    for api in project["apis"]:
        if "auth" not in api:  # public API, nothing to look up
            continue
        ref = api["auth"]["secretRef"]
        if not ref.startswith("secret://"):
            raise InstallError(f"{api['id']}: secretRef must start with secret://")
        if not keyring.get_password(KEYCHAIN_SERVICE, ref.removeprefix("secret://")):
            raise InstallError(
                f"Secret {ref} isn't in the keychain yet. Store it with:\n"
                f"  uv run python -c \"import keyring, getpass; keyring.set_password("
                f"'{KEYCHAIN_SERVICE}', '{ref.removeprefix('secret://')}', getpass.getpass('Value: '))\"")
    return project


def verify_runtime(project_path):
    """Start the runtime the same way Desktop will and list its tools."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(RUNTIME), "--project", str(project_path)],
            env=dict(os.environ),  # closer to what Desktop passes than the SDK's trimmed default
        )
        with anyio.fail_after(30):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return (await session.list_tools()).tools

    try:
        return anyio.run(run)
    except Exception:
        raise InstallError(f"The runtime failed to start or list its tools. Its error output, "
                           f"if any, is above. To see it on its own, run:\n"
                           f"  uv run runtime.py --project {project_path}") from None


# ------------------------------------------------------- build the entry ---

def build_entry(project_path):
    # Absolute paths only: Desktop doesn't use your terminal's PATH, and a
    # relative path would resolve against whatever folder Desktop starts in.
    # sys.executable is this project's .venv Python, which has every package installed.
    return {
        "command": sys.executable,
        "args": [str(RUNTIME), "--project", str(project_path)],
        # no "env": the runtime reads its secrets from the keychain itself
    }


def is_ours(entry):
    args = entry.get("args", [])
    return any(str(a).endswith("runtime.py") for a in args) and "--project" in args


def diff_text(before, after, path):
    diff = difflib.unified_diff(
        to_text(before).splitlines(keepends=True),
        to_text(after).splitlines(keepends=True),
        fromfile=f"{path} (current)", tofile=f"{path} (new)", n=2)
    return "".join(diff) or "(no changes)"


def show_diff(before, after, path):
    print(diff_text(before, after, path))


def write_config(config_path, original_text, new_config):
    # Abort if something else changed the file since we read it
    if config_path.read_text(encoding="utf-8") != original_text:
        raise InstallError("The config file changed while we were working. Nothing was written; run again.")

    backup_path = backup(config_path)
    atomic_write(config_path, to_text(new_config))

    # Re-read and parse what we wrote
    try:
        written = read_json(config_path)
    except json.JSONDecodeError:
        shutil.copy2(backup_path, config_path)
        raise InstallError("Written file didn't parse, so the backup was restored.")
    if written != new_config:
        shutil.copy2(backup_path, config_path)
        raise InstallError("Written file didn't match, so the backup was restored.")
    return backup_path


def record_registration(project_path, project, record):
    client = record.get("client", CLIENT_ID)
    clients = [c for c in project.get("clients", [])
               if not (c.get("client") == client and c.get("configPath") == record.get("configPath"))]
    if record.get("status") == "registered":
        clients.append(record)
    project["clients"] = clients
    atomic_write(project_path, to_text(project))


def load_config(config_path):
    text = config_path.read_text(encoding="utf-8")
    try:
        return text, json.loads(text)
    except json.JSONDecodeError as e:
        # Never write over a file we couldn't parse: we'd destroy the user's settings
        raise InstallError(f"{config_path} isn't valid JSON ({e}). Fix it by hand first; nothing was changed.")


def warn_if_running(assume_yes):
    if desktop_is_running():
        print("Claude Desktop is running. It saves its own settings to this same file "
              "and can overwrite the change.")
        if not confirm("Quit Desktop first (recommended). Continue anyway?", assume_yes):
            raise InstallError("Cancelled. Quit Claude Desktop and run again.")


def restart_hint():
    if platform.system() == "Windows":
        print("Restart Claude Desktop to load it:\n"
              "  Get-Process claude -ErrorAction SilentlyContinue | Stop-Process\n"
              "then open Claude again.")
    else:
        print("Fully quit and reopen Claude Desktop to load it.")


# --------------------------------------------------------------- commands ---

def cmd_add(opts):
    project_path = Path(opts.project).resolve()
    project = check_project(project_path)
    name = project["name"]
    print(f"Project '{name}' OK, secrets found in the keychain.")

    if not opts.skip_verify:
        print("Starting the runtime to check it...")
        tools = verify_runtime(project_path)
        print(f"Runtime OK. Claude will get {len(tools)} tool(s):")
        for t in tools:
            print(f"  - {t.name}: {t.description}")

    config_path = find_config(opts.config, opts.yes)
    print(f"\nConfig: {config_path}")
    warn_if_running(opts.yes)

    original_text, config = load_config(config_path)
    servers = config.get("mcpServers", {})
    entry = build_entry(project_path)

    if name in servers:
        if servers[name] == entry:
            print(f"'{name}' is already registered with these exact settings. Nothing to do.")
            return
        if not is_ours(servers[name]):
            print(f"An entry named '{name}' already exists and wasn't made by this installer:")
            print("  " + json.dumps(servers[name]))
            if not confirm("Replace it?", opts.yes):
                raise InstallError("Cancelled. Rename the project ('name' in project.json) to avoid the clash.")

    new_config = json.loads(json.dumps(config))  # deep copy
    new_config.setdefault("mcpServers", {})[name] = entry

    print()
    show_diff(config, new_config, config_path.name)
    if not confirm("\nWrite this change?", opts.yes):
        raise InstallError("Cancelled. Nothing was written.")

    backup_path = write_config(config_path, original_text, new_config)
    record_registration(project_path, project, {
        "client": CLIENT_ID,
        "configPath": str(config_path),
        "serverName": name,
        "backupPath": str(backup_path),
        "status": "registered",
        "registeredAt": datetime.now().isoformat(timespec="seconds"),
    })
    print(f"\nRegistered '{name}'. Backup of the old config: {backup_path}")
    restart_hint()


def cmd_remove(opts):
    project_path = Path(opts.project).resolve()
    project = read_json(project_path)
    name = project["name"]

    recorded = [c for c in project.get("clients", []) if c.get("client") == CLIENT_ID]
    config_arg = opts.config or (recorded[0]["configPath"] if recorded else None)
    config_path = find_config(config_arg, opts.yes)
    print(f"Config: {config_path}")
    warn_if_running(opts.yes)

    original_text, config = load_config(config_path)
    servers = config.get("mcpServers", {})
    if name not in servers:
        print(f"'{name}' isn't in this config. Nothing to do.")
        record_registration(project_path, project, {"configPath": str(config_path), "status": "removed"})
        return
    if not is_ours(servers[name]) and not confirm(
            f"'{name}' wasn't made by this installer. Remove it anyway?", opts.yes):
        raise InstallError("Cancelled. Nothing was changed.")

    new_config = json.loads(json.dumps(config))
    del new_config["mcpServers"][name]

    show_diff(config, new_config, config_path.name)
    if not confirm("\nWrite this change?", opts.yes):
        raise InstallError("Cancelled. Nothing was written.")

    backup_path = write_config(config_path, original_text, new_config)
    record_registration(project_path, project, {"configPath": str(config_path), "status": "removed"})
    print(f"\nRemoved '{name}'. Backup of the old config: {backup_path}")
    restart_hint()


def cmd_add_code(opts):
    import claude_code

    if opts.url:
        return cmd_add_code_remote(opts)
    project_path = Path(opts.project).resolve()
    project = check_project(project_path)
    name = project["name"]
    print(f"Project '{name}' OK, secrets found in the keychain.")
    if not opts.skip_verify:
        print("Starting the runtime to check it...")
        tools = verify_runtime(project_path)
        print(f"Runtime OK. Claude will get {len(tools)} tool(s):")
        for t in tools:
            print(f"  - {t.name}: {t.description}")

    scope = opts.scope or "user"
    cwd = claude_code.check_target(scope, opts.folder)
    print(f"\nClaude Code, {scope} scope" + ("" if scope == "user" else f", folder {cwd}"))
    for warning in claude_code.warnings_for(scope):
        print(f"Warning: {warning}")
    print("Will run:\n  " + claude_code.display(claude_code.add_args(name, project_path, scope)))
    if not confirm("\nRun it?", opts.yes):
        raise InstallError("Cancelled. Nothing was changed.")

    print(claude_code.add(name, project_path, scope, opts.folder))
    record_registration(project_path, project, {
        "client": claude_code.CLIENT_ID,
        "configPath": claude_code.registration_key(scope, opts.folder),
        "scope": scope,
        "folder": None if scope == "user" else str(cwd),
        "serverName": name,
        "status": "registered",
        "registeredAt": datetime.now().isoformat(timespec="seconds"),
    })
    print("\nDone. Start a new Claude Code session and run /mcp to see it."
          + (" Claude Code will ask you to approve project-scope servers." if scope == "project" else ""))


def cmd_add_code_remote(opts):
    import claude_code
    import remote

    project_path = Path(opts.project).resolve()
    project = read_json(project_path)
    url = remote.validate_url(opts.url)
    token = remote.get_token(project)
    if not token:
        raise InstallError("No inbound token yet. Run: uv run remote.py token --project " + opts.project)
    name = claude_code.remote_name(project["name"])

    if not opts.skip_verify:
        print(f"Connecting to {url} ...")
        tools, _ = remote.remote_session(url, token)
        print(f"Remote server OK. Claude will get {len(tools)} tool(s):")
        for t in tools:
            print(f"  - {t.name}")

    scope = opts.scope or "user"
    cwd = claude_code.check_target(scope, opts.folder)
    if scope == "project":
        print("Warning: project scope would put your bearer token in .mcp.json, which is meant to be "
              "committed. Use user or local scope for remote servers.")
        if not confirm("Continue anyway?", opts.yes):
            raise InstallError("Cancelled. Nothing was changed.")
    print("Will run:\n  " + claude_code.display(claude_code.add_http_args(name, url, token, scope)))
    if not confirm("\nRun it?", opts.yes):
        raise InstallError("Cancelled. Nothing was changed.")

    print(claude_code.add(name, project_path, scope, opts.folder, url=url, token=token))
    project["deployment"] = {**project.get("deployment", {}), "mode": "remote", "url": url}
    record_registration(project_path, project, {
        "client": claude_code.CLIENT_ID,
        "configPath": claude_code.registration_key(scope, opts.folder, remote=True),
        "scope": scope, "folder": None if scope == "user" else str(cwd), "remote": True,
        "serverName": name, "url": url, "status": "registered",
        "registeredAt": datetime.now().isoformat(timespec="seconds"),
    })
    print(f"\nDone. '{name}' points at {url}. Start a new Claude Code session and run /mcp to see it.")


def cmd_remove_code(opts):
    import claude_code

    project_path = Path(opts.project).resolve()
    project = read_json(project_path)
    remote_entry = bool(opts.remote)
    name = claude_code.remote_name(project["name"]) if remote_entry else project["name"]

    scope, folder = opts.scope, opts.folder
    if not scope:  # fall back to what was recorded at install time
        recorded = [c for c in project.get("clients", []) if c.get("client") == claude_code.CLIENT_ID
                    and bool(c.get("remote")) == remote_entry]
        if len(recorded) != 1:
            raise InstallError("Pass --scope (and --folder for local or project scope): "
                               + ("no Claude Code registration is recorded."
                                  if not recorded else "this project is registered in more than one place."))
        scope, folder = recorded[0]["scope"], recorded[0].get("folder")

    print("Will run:\n  " + claude_code.display(claude_code.remove_args(name, scope)))
    if not confirm("Run it?", opts.yes):
        raise InstallError("Cancelled. Nothing was changed.")
    print(claude_code.remove(name, scope, folder))
    record_registration(project_path, project, {
        "client": claude_code.CLIENT_ID,
        "configPath": claude_code.registration_key(scope, folder, remote=remote_entry),
        "status": "removed",
    })


def cmd_list(opts):
    paths = [Path(opts.config)] if opts.config else candidate_configs()
    if not paths:
        print("No Claude Desktop config found.")
        return
    for path in paths:
        print(path)
        try:
            servers = read_json(path).get("mcpServers", {})
        except json.JSONDecodeError as e:
            print(f"  (invalid JSON: {e})")
            continue
        if not servers:
            print("  (no MCP servers)")
        for name, entry in servers.items():
            mark = "  [installer]" if is_ours(entry) else ""
            print(f"  - {name}: {entry.get('command')}{mark}")


def main():
    parser = argparse.ArgumentParser(description="Register an MCP project with Claude Desktop or Claude Code")
    sub = parser.add_subparsers(dest="command", required=True)

    for cmd in ("add", "remove"):
        p = sub.add_parser(cmd)
        p.add_argument("--project", required=True, help="Path to project.json")
        p.add_argument("--client", choices=["claude-desktop", "claude-code"], default="claude-desktop")
        p.add_argument("--config", help="Claude Desktop: path to claude_desktop_config.json")
        p.add_argument("--scope", choices=["user", "local", "project"],
                       help="Claude Code: where to register (default: user)")
        p.add_argument("--folder", help="Claude Code: project folder for local or project scope")
        if cmd == "add":
            p.add_argument("--url", help="Claude Code: register a remote server at this URL (ends in /mcp)")
        else:
            p.add_argument("--remote", action="store_true", help="Claude Code: remove the remote registration")
        p.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
        if cmd == "add":
            p.add_argument("--skip-verify", action="store_true", help="Don't test-start the runtime")

    p = sub.add_parser("list")
    p.add_argument("--config", help="Path to claude_desktop_config.json")

    opts = parser.parse_args()
    # When this file runs as a script it is __main__, and claude_code imports it again as
    # "install", so its InstallError is a second class with the same name. Catch both.
    import claude_code
    errors = (InstallError, claude_code.InstallError)
    try:
        if opts.command in ("add", "remove") and opts.client == "claude-code":
            {"add": cmd_add_code, "remove": cmd_remove_code}[opts.command](opts)
        else:
            {"add": cmd_add, "remove": cmd_remove, "list": cmd_list}[opts.command](opts)
    except errors as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
