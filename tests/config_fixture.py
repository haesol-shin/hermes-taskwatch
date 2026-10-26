"""Synthetic config objects. Hosts and paths are placeholders."""

import json
from pathlib import Path

from taskwatch.config import Config, load_config

ROOT = "/placeholder/hermes"
BOARD = "board.invalid"


def config_dict() -> dict:
    return {
        "version": 1,
        "hermes_root": ROOT,
        "board": BOARD,
        "profiles": ["main", "worker"],
        "destinations": {
            "main_subscription": "main.invalid",
            "user_notify": "user.invalid",
        },
        "executors": {
            "work": ["executor", "--brief", "{brief}", "--copy", "{copy}", "--output", "{output}"],
            "review": ["executor", "--review", "--brief", "{brief}", "--output", "{output}"],
        },
        "times": {
            "gate_action_seconds": 1800,
            "reject_followup_extra_seconds": 3600,
            "next_start_seconds": 1800,
            "unstarted_after_parents_seconds": 3600,
            "start_approval_without_card_seconds": 1800,
            "rewarn_initial_seconds": 1800,
            "rewarn_backoff": 2,
            "user_notify_unreceived_seconds": 600,
            "stall_unchanged_seconds": 1800,
        },
        "limits": {"reject_cap": 3, "rewarn_cap": 3},
        "comments": {
            "handled_prefix": "handled event=",
            "received_prefix": "received alert=",
            "approve": "approve",
            "reject": "reject",
            "wait_prefix": "wait:",
            "next_prefix": "next:",
        },
        "watch_hook": False,
    }


def write_config(path: Path, raw: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw if raw is not None else config_dict()), encoding="utf-8")
    return path


def loaded(path: Path) -> Config:
    return load_config(path)
