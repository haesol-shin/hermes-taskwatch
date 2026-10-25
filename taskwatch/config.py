"""Frozen config for one Hermes root, one board, and one profile list.

Unknown keys and missing keys are refused before a watch starts. A silent
default would hide a file that points at the wrong target.
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG_VERSION = 1

_TOP_KEYS = frozenset(
    {
        "version",
        "hermes_root",
        "board",
        "profiles",
        "destinations",
        "executors",
        "times",
        "limits",
        "comments",
    }
)
_DESTINATION_KEYS = frozenset({"main_subscription", "user_notify"})
_EXECUTOR_KEYS = frozenset({"work", "review"})
_TIME_KEYS = frozenset(
    {
        "gate_action_seconds",
        "reject_followup_extra_seconds",
        "next_start_seconds",
        "unstarted_after_parents_seconds",
        "start_approval_without_card_seconds",
        "rewarn_initial_seconds",
        "rewarn_backoff",
        "user_notify_unreceived_seconds",
        "stall_unchanged_seconds",
    }
)
_LIMIT_KEYS = frozenset({"reject_cap", "rewarn_cap"})
_COMMENT_KEYS = frozenset(
    {
        "handled_prefix",
        "received_prefix",
        "approve",
        "reject",
        "wait_prefix",
        "next_prefix",
    }
)


class ConfigError(ValueError):
    """The config cannot be used. Callers must not treat this as a clean watch."""


@dataclass(frozen=True, slots=True)
class Destinations:
    main_subscription: str
    user_notify: str


@dataclass(frozen=True, slots=True)
class Executors:
    work: tuple[str, ...]
    review: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Times:
    gate_action_seconds: int
    reject_followup_extra_seconds: int
    next_start_seconds: int
    unstarted_after_parents_seconds: int
    start_approval_without_card_seconds: int
    rewarn_initial_seconds: int
    rewarn_backoff: int
    user_notify_unreceived_seconds: int
    stall_unchanged_seconds: int


@dataclass(frozen=True, slots=True)
class Limits:
    reject_cap: int
    rewarn_cap: int


@dataclass(frozen=True, slots=True)
class CommentRules:
    handled_prefix: str
    received_prefix: str
    approve: str
    reject: str
    wait_prefix: str
    next_prefix: str


@dataclass(frozen=True, slots=True)
class Config:
    version: int
    hermes_root: str
    board: str
    profiles: tuple[str, ...]
    destinations: Destinations
    executors: Executors
    times: Times
    limits: Limits
    comments: CommentRules


def _native_hermes_home() -> Path:
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def _expand_hermes_home(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path)))


def hermes_root() -> Path:
    """Root Hermes dir, including when HERMES_HOME is ``<root>/profiles/<name>``.

    Reimplemented from Hermes ``get_default_hermes_root`` (hermes_constants.py
    lines 217-234). The memo is omitted so a changed HERMES_HOME is visible.
    Do not import Hermes.
    """
    native_home = _native_hermes_home()
    env_home = os.environ.get("HERMES_HOME", "").strip()
    env_path = _expand_hermes_home(env_home) if env_home else None
    result = native_home
    if env_path is not None:
        try:
            env_path.resolve().relative_to(native_home.resolve())
        except ValueError:
            result = env_path.parent.parent if env_path.parent.name == "profiles" else env_path
    return result


def config_path(root: Path) -> Path:
    return root / "taskwatch" / "config.json"


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError("unreadable config") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("config is not json") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be an object")
    _exact_keys(raw, _TOP_KEYS, "config")
    version = raw["version"]
    if isinstance(version, bool) or version != CONFIG_VERSION:
        raise ConfigError("unknown config version")
    return Config(
        version=CONFIG_VERSION,
        hermes_root=_absolute(raw["hermes_root"], "hermes_root"),
        board=_text(raw["board"], "board"),
        profiles=_profiles(raw["profiles"]),
        destinations=_destinations(raw["destinations"]),
        executors=_executors(raw["executors"]),
        times=_times(raw["times"]),
        limits=_limits(raw["limits"]),
        comments=_comments(raw["comments"]),
    )


def _exact_keys(raw: dict, allowed: frozenset[str], where: str) -> None:
    missing = allowed - raw.keys()
    unknown = raw.keys() - allowed
    if missing:
        raise ConfigError(f"{where}: missing {sorted(missing)[0]}")
    if unknown:
        raise ConfigError(f"{where}: unknown {sorted(unknown)[0]}")


def _object(value: object, key: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object")
    return value


def _text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _absolute(value: object, key: str) -> str:
    text = _text(value, key)
    if not Path(text).is_absolute():
        raise ConfigError(f"{key} must be absolute")
    return text


def _positive_int(value: object, key: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


def _profiles(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("profiles must be a non-empty list")
    names = tuple(_text(item, "profiles") for item in value)
    if len(set(names)) != len(names):
        raise ConfigError("profiles has a duplicate")
    return names


def _destinations(value: object) -> Destinations:
    raw = _object(value, "destinations")
    _exact_keys(raw, _DESTINATION_KEYS, "destinations")
    return Destinations(
        main_subscription=_text(raw["main_subscription"], "destinations.main_subscription"),
        user_notify=_text(raw["user_notify"], "destinations.user_notify"),
    )


def _arg_list(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key} must be a non-empty argument list")
    args = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ConfigError(f"{key} must contain only non-empty strings")
        args.append(item)
    return tuple(args)


def _executors(value: object) -> Executors:
    raw = _object(value, "executors")
    _exact_keys(raw, _EXECUTOR_KEYS, "executors")
    return Executors(
        work=_arg_list(raw["work"], "executors.work"),
        review=_arg_list(raw["review"], "executors.review"),
    )


def _times(value: object) -> Times:
    raw = _object(value, "times")
    _exact_keys(raw, _TIME_KEYS, "times")
    values = {key: _positive_int(raw[key], f"times.{key}") for key in _TIME_KEYS}
    values["rewarn_backoff"] = _positive_int(
        raw["rewarn_backoff"], "times.rewarn_backoff", minimum=2
    )
    return Times(**values)


def _limits(value: object) -> Limits:
    raw = _object(value, "limits")
    _exact_keys(raw, _LIMIT_KEYS, "limits")
    return Limits(
        reject_cap=_positive_int(raw["reject_cap"], "limits.reject_cap"),
        rewarn_cap=_positive_int(raw["rewarn_cap"], "limits.rewarn_cap"),
    )


def _comments(value: object) -> CommentRules:
    raw = _object(value, "comments")
    _exact_keys(raw, _COMMENT_KEYS, "comments")
    return CommentRules(**{key: _text(raw[key], f"comments.{key}") for key in _COMMENT_KEYS})
