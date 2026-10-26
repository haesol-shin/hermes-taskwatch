import json

import pytest

from taskwatch.config import ConfigError, config_path, load_config
from tests.config_fixture import BOARD, ROOT, config_dict, write_config


def test_config_path_is_under_resolved_root(tmp_path):
    assert config_path(tmp_path) == tmp_path / "taskwatch" / "config.json"


def test_load_keeps_target_commands_and_times(tmp_path):
    path = write_config(tmp_path / "config.json")
    cfg = load_config(path)

    assert cfg.version == 1
    assert cfg.hermes_root == ROOT
    assert cfg.board == BOARD
    assert cfg.profiles == ("main", "worker")
    assert cfg.destinations.main_subscription == "main.invalid"
    assert cfg.destinations.user_notify == "user.invalid"
    assert cfg.executors.work[0] == "executor"
    assert "{output}" in cfg.executors.work
    assert cfg.executors.review[1] == "--review"
    assert cfg.times.gate_action_seconds == 1800
    assert cfg.times.rewarn_backoff == 2
    assert cfg.limits.reject_cap == 3
    assert cfg.comments.approve == "approve"
    assert cfg == load_config(path)


def test_missing_key_is_refused(tmp_path):
    raw = config_dict()
    del raw["board"]
    write_config(tmp_path / "config.json", raw)

    with pytest.raises(ConfigError, match="board"):
        load_config(tmp_path / "config.json")


def test_unknown_key_is_refused(tmp_path):
    raw = config_dict()
    raw["room"] = "secret.invalid"
    write_config(tmp_path / "config.json", raw)

    with pytest.raises(ConfigError, match="room"):
        load_config(tmp_path / "config.json")


def test_unknown_nested_time_key_is_refused(tmp_path):
    raw = config_dict()
    raw["times"]["scan_interval_seconds"] = 300
    write_config(tmp_path / "config.json", raw)

    with pytest.raises(ConfigError, match="scan_interval_seconds"):
        load_config(tmp_path / "config.json")


def test_unknown_version_is_refused(tmp_path):
    raw = config_dict()
    raw["version"] = 2
    write_config(tmp_path / "config.json", raw)

    with pytest.raises(ConfigError, match="version"):
        load_config(tmp_path / "config.json")


def test_shell_string_command_is_refused(tmp_path):
    raw = config_dict()
    raw["executors"]["work"] = "executor --brief {brief}"
    write_config(tmp_path / "config.json", raw)

    with pytest.raises(ConfigError, match="executors.work"):
        load_config(tmp_path / "config.json")


def test_relative_root_and_empty_profile_are_refused(tmp_path):
    raw = config_dict()
    raw["hermes_root"] = "placeholder/hermes"
    write_config(tmp_path / "config.json", raw)
    with pytest.raises(ConfigError, match="hermes_root"):
        load_config(tmp_path / "config.json")

    raw = config_dict()
    raw["profiles"] = ["main", "main"]
    write_config(tmp_path / "config.json", raw)
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(tmp_path / "config.json")


def test_backoff_must_increase_interval(tmp_path):
    raw = config_dict()
    raw["times"]["rewarn_backoff"] = 1
    write_config(tmp_path / "config.json", raw)

    with pytest.raises(ConfigError, match="rewarn_backoff"):
        load_config(tmp_path / "config.json")


def test_missing_file_is_not_an_empty_config(tmp_path):
    with pytest.raises(ConfigError, match="unreadable"):
        load_config(tmp_path / "missing.json")


def test_non_object_json_is_refused(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps([1]), encoding="utf-8")
    with pytest.raises(ConfigError, match="object"):
        load_config(path)


def test_undecodable_config_is_a_config_error(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="unreadable"):
        load_config(path)
