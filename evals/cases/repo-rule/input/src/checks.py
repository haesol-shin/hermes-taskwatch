"""Checks over board cards."""

import json
import urllib.request

from src.board import Board

STALE_AFTER_SECONDS = 7 * 24 * 3600


def check_has_owner(board: Board) -> list[str]:
    return [card["id"] for card in board.cards() if not card.get("owner")]


def check_stale(board: Board, now: float) -> list[str]:
    stale = []
    for card in board.cards():
        url = f"https://board.example.invalid/api/cards/{card['id']}"
        with urllib.request.urlopen(url, timeout=5) as response:
            updated_at = json.load(response)["updated_at"]
        if now - updated_at > STALE_AFTER_SECONDS:
            stale.append(card["id"])
    return stale
