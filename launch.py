"""Starts an MCP server installed from the MCP Registry, with its secrets.

Claude Desktop and Claude Code run:
    python launch.py --spec ~/.mcpgui/servers/<name>.json

The spec (written by registry.py) holds the command, its arguments and plain
environment variables, plus secret:// references for anything secret. This
script reads those from the environment (MCP_SECRET_<ACCOUNT>) or the OS
keychain, then runs the server with the client's stdin and stdout. So API keys
for third-party servers stay out of the clients' config files too.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

KEYCHAIN_SERVICE = "mcp-setup-gui"


def log(message):
    print(f"[launch] {message}", file=sys.stderr)  # stdout belongs to MCP


def read_secret(ref):
    account = ref.removeprefix("secret://")
    value = os.environ.get("MCP_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", account).upper())
    if value:
        return value
    try:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, account)
    except Exception:
        return None


def resolve(value):
    """A plain string, or {"secretRef": "secret://..."}."""
    if isinstance(value, dict):
        secret = read_secret(value["secretRef"])
        if secret is None:
            raise SystemExit(f"[launch] Missing secret {value['secretRef']}. Reinstall the server from "
                             "MCP Setup to store it again.")
        return secret
    return str(value)


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "--spec":
        raise SystemExit("usage: launch.py --spec PATH")
    spec = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

    env = dict(os.environ)
    env.update({k: str(v) for k, v in spec.get("env", {}).items()})
    for name, ref in spec.get("secretEnv", {}).items():
        env[name] = resolve({"secretRef": ref})
    args = [resolve(a) for a in spec.get("args", [])]

    command = spec["command"]
    if not Path(command).is_file():
        import shutil
        found = shutil.which(command, path=env.get("PATH"))
        if not found:
            raise SystemExit(f"[launch] {spec.get('needs') or command} isn't installed or isn't on PATH, "
                             f"so {spec.get('registryName', 'this server')} can't start.")
        command = found

    log(f"starting {spec.get('registryName', '?')} {spec.get('version', '')}")
    try:
        # stdin and stdout are inherited: the client talks to the server directly
        process = subprocess.Popen([command, *args], env=env)
        sys.exit(process.wait())
    except KeyboardInterrupt:
        sys.exit(130)
    except OSError as e:
        raise SystemExit(f"[launch] couldn't start {command}: {e.strerror or e}")


if __name__ == "__main__":
    main()
