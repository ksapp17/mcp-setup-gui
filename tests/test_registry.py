import json

import pytest

import install
import registry

ECHO = {"server": {"name": "io.github.test/echo", "version": "1.2.3", "packages": [
    {"registryType": "npm", "identifier": "@test/echo", "version": "1.2.3", "transport": {"type": "stdio"},
     "packageArguments": [
         {"type": "named", "name": "--mode", "value": "{mode}", "variables": {"mode": {"default": "fast"}}},
         {"type": "positional", "valueHint": "token", "isRequired": True, "isSecret": True}],
     "environmentVariables": [{"name": "API_KEY", "isRequired": True, "isSecret": True},
                              {"name": "LOG", "default": "info"}]},
    {"registryType": "oci", "identifier": "ghcr.io/test/echo", "version": "1.2.3", "transport": {"type": "stdio"},
     "environmentVariables": [{"name": "API_KEY", "isSecret": True}]},
    {"registryType": "mcpb", "identifier": "https://x/y.mcpb", "version": "1", "transport": {"type": "stdio"}}],
    "remotes": [{"type": "streamable-http", "url": "https://{region}.example.com/mcp",
                 "variables": {"region": {"isRequired": True}},
                 "headers": [{"name": "Authorization", "value": "Bearer {t}", "variables": {"t": {"isSecret": True}}}]}]}}


def opt(option_id):
    return registry.find_option(ECHO["server"], option_id)


def test_options_and_unsupported():
    ids = {o["id"]: o for o in registry.options(ECHO["server"])}
    assert "unsupported" in ids["p2"]  # mcpb
    assert "unsupported" not in ids["r0"]


def test_npm_plan_keeps_secrets_out_of_spec():
    values = {"arg:1": "TOKVAL", "env:API_KEY": "KEYVAL"}
    spec, secrets = registry.plan_package(ECHO, opt("p0"), values, "echo")
    text = json.dumps(spec)
    assert "TOKVAL" not in text and "KEYVAL" not in text
    assert spec["args"][:4] == ["-y", "@test/echo@1.2.3", "--mode", "fast"]
    assert spec["args"][4] == {"secretRef": "secret://registry/echo/arg1"}
    assert spec["env"] == {"LOG": "info"} and spec["secretEnv"] == {"API_KEY": "secret://registry/echo/API_KEY"}
    assert set(secrets.values()) == {"TOKVAL", "KEYVAL"}


def test_required_value_missing():
    with pytest.raises(registry.RegistryError, match="API_KEY"):
        registry.plan_package(ECHO, opt("p0"), {"arg:1": "T"}, "echo")


def test_docker_passes_env_by_name():
    spec, _ = registry.plan_package(ECHO, opt("p1"), {"env:API_KEY": "K"}, "echo")
    assert spec["args"] == ["run", "-i", "--rm", "-e", "API_KEY", "ghcr.io/test/echo:1.2.3"]


def test_remote_plan_fills_url_and_header():
    spec, headers = registry.plan_remote(ECHO, opt("r0"), {"url:region": "eu", "header:Authorization:t": "abc"})
    assert spec["remote"]["url"] == "https://eu.example.com/mcp" and headers == {"Authorization": "Bearer abc"}


def test_url_value_cannot_change_host():
    spec, _ = registry.plan_remote(ECHO, opt("r0"), {"url:region": "evil.com/x?"})
    assert spec["remote"]["url"].startswith("https://evil.com%2Fx%3F.example.com")


def test_bad_names():
    with pytest.raises(registry.RegistryError):
        registry.fetch("../../etc/passwd")
    for bad in ("", "a b", "x" * 65, "../x"):
        with pytest.raises(registry.RegistryError):
            registry.check_local_name(bad)


def test_desktop_install_and_remove(tmp_path, keychain, monkeypatch):
    monkeypatch.setattr(registry, "SPEC_DIR", tmp_path / "servers")
    monkeypatch.setattr(install, "BACKUP_DIR", tmp_path / "backups")
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    spec, secrets = registry.plan_package(ECHO, opt("p0"), {"arg:1": "T", "env:API_KEY": "KEYVAL"}, "echo")
    original, current, new = registry.desktop_change("echo", config)
    registry.install_desktop("echo", spec, secrets, config, original, new)

    written = json.loads(config.read_text())
    assert written["keep"] == 1 and "other" in written["mcpServers"]
    assert written["mcpServers"]["echo"]["args"][0].endswith("launch.py")
    assert keychain[("mcp-setup-gui", "registry/echo/API_KEY")] == "KEYVAL"
    assert "KEYVAL" not in config.read_text()

    registry.remove("echo")
    assert "echo" not in json.loads(config.read_text())["mcpServers"]
    assert not any(k[1].startswith("registry/echo") for k in keychain)


def test_wont_replace_someone_elses_entry(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"echo": {"command": "theirs"}}}))
    with pytest.raises(registry.RegistryError, match="didn't add"):
        registry.desktop_change("echo", config)
