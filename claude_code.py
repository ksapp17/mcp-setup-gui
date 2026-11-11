"""Claude Code adapter: registers a project with Claude Code through its own CLI.

It runs `claude mcp add` / `claude mcp remove` instead of editing Claude Code's
files. ~/.claude.json holds all of Claude Code's state and is rewritten while it
runs, so going through the CLI avoids racing it.

Scopes (Claude Code's own):
  user     every project on this machine           (stored in ~/.claude.json)
  local    one project folder, only for you          (~/.claude.json, keyed by folder)
  project  one project folder, shared through git    (.mcp.json in that folder)
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from install import RUNTIME, InstallError

CLIENT_ID = "claude-code"
SCOPES = ("user", "local", "project")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def find_cli():
    path = shutil.which("claude")
    if not path:
        raise InstallError("Claude Code's `claude` command isn't on PATH. Install Claude Code, "
                           "or open a new terminal if you just did.")
    return path


def check_target(scope, folder):
    """Validate scope and folder. Returns the folder the CLI must run in."""
    if scope not in SCOPES:
        raise InstallError(f"Scope must be one of: {', '.join(SCOPES)}")
    if scope == "user":
        return Path.home()
    if not folder:
        raise InstallError(f"The {scope} scope belongs to one project folder. Choose the folder "
                           "you open Claude Code in.")
    path = Path(folder).expanduser().resolve()
    if not path.is_dir():
        raise InstallError(f"No folder at {path}")
    return path


def run(args, cwd):
    try:
        result = subprocess.run([find_cli(), *args], cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        raise InstallError("`claude " + " ".join(args[:2]) + "` didn't finish within 2 minutes.")
    output = ANSI.sub("", (result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode, output


def add_args(name, project_path, scope):
    # Options go before the name; everything after "--" is the server's own command line.
    # Absolute paths only, and no -e secrets: the runtime reads the keychain itself.
    return ["mcp", "add", "--transport", "stdio", "--scope", scope, name, "--",
            sys.executable, str(RUNTIME), "--project", str(project_path)]


def add_http_args(name, url, token, scope):
    # Remote server: Claude Code connects to the URL and sends the bearer token on every request.
    # Claude Code keeps this header in its own config file, in plain text.
    return ["mcp", "add", "--transport", "http", "--scope", scope, name, url,
            "--header", f"Authorization: Bearer {token}"]


TOKEN_PATTERN = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-.~+/=]+")


def mask(text):
    """Hide bearer tokens in anything shown to the user."""
    return TOKEN_PATTERN.sub(r"\1••••••••", text)


def remove_args(name, scope):
    return ["mcp", "remove", "--scope", scope, name]


def display(args):
    """The command as the user would type it, for previews."""
    parts = ["claude", *args]
    return mask(subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts))


def is_ours(get_output):
    return RUNTIME.name in get_output and "--project" in get_output


def get(name, cwd):
    """`claude mcp get` output for a server, or None if it doesn't exist."""
    code, output = run(["mcp", "get", name], cwd)
    return mask(output) if code == 0 else None


def warnings_for(scope):
    if scope == "project":
        return ["Project scope writes this machine's absolute paths into .mcp.json, which is meant to be "
                "committed. Teammates would need the same paths, and Claude Code asks each person to "
                "approve the server. Use local or user scope unless you've planned for that."]
    return []


def add(name, project_path, scope, folder, replace_foreign=False, url=None, token=None):
    """Register the project: locally (stdio) by default, or as a remote server when url is given.
    Replaces an earlier registration made by this tool in the same scope."""
    cwd = check_target(scope, folder)
    args = add_http_args(name, url, token, scope) if url else add_args(name, project_path, scope)
    code, output = run(args, cwd)
    if code == 0:
        return mask(output)

    if "already exists" not in output:
        raise InstallError(f"claude mcp add failed:\n{mask(output)}")

    # Same name in the same scope: replace it only if it's ours, or the user said so
    existing = get(name, cwd) or ""
    ours = (url in existing) if url else is_ours(existing)
    if not ours and not replace_foreign:
        raise InstallError(f"Claude Code already has a server named '{name}' in {scope} scope that this "
                           f"tool didn't create:\n{existing}\nRemove it first, or rename the project.")
    code, removed = run(remove_args(name, scope), cwd)
    if code != 0:
        raise InstallError(f"Couldn't replace the existing '{name}':\n{removed}")
    code, output = run(args, cwd)
    if code != 0:
        raise InstallError(f"claude mcp add failed after removing the old entry:\n{mask(output)}")
    return f"Replaced the earlier '{name}'.\n{mask(output)}"


def remove(name, scope, folder):
    cwd = check_target(scope, folder)
    code, output = run(remove_args(name, scope), cwd)
    if code != 0:
        raise InstallError(f"claude mcp remove failed:\n{output}")
    return output


def registration_key(scope, folder, remote=False):
    """Where a registration lives, used to match records in project.json."""
    target = "~" if scope == "user" else str(check_target(scope, folder))
    return f"claude-code{'-remote' if remote else ''}:{scope}:{target}"


def remote_name(project_name):
    # A different name from the local registration, so both can exist side by side
    return f"{project_name}-remote"
