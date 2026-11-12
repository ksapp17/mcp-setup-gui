import json

import pytest

import install


def test_write_config_backs_up_and_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "BACKUP_DIR", tmp_path / "backups")
    config = tmp_path / "claude_desktop_config.json"
    config.write_text('{"a": 1}')
    backup = install.write_config(config, '{"a": 1}', {"a": 1, "mcpServers": {}})
    assert json.loads(config.read_text())["mcpServers"] == {}
    assert backup.read_text() == '{"a": 1}'


def test_write_config_refuses_if_file_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "BACKUP_DIR", tmp_path / "backups")
    config = tmp_path / "claude_desktop_config.json"
    config.write_text('{"a": 2}')
    with pytest.raises(install.InstallError, match="changed"):
        install.write_config(config, '{"a": 1}', {"a": 3})
    assert config.read_text() == '{"a": 2}'


def test_never_writes_over_broken_json(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text('{"a": 1,}')
    with pytest.raises(install.InstallError, match="isn't valid JSON"):
        install.load_config(config)


def test_backups_get_unique_names(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "BACKUP_DIR", tmp_path / "backups")
    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{}")
    assert install.backup(config) != install.backup(config)


def test_entry_uses_absolute_paths(tmp_path):
    entry = install.build_entry(tmp_path / "p.json")
    assert install.is_ours(entry)
    assert all(not a.startswith(".") for a in entry["args"])
