"""Collect new notices from https://board.example.invalid/notices."""


def new_notices(listing: list[dict], seen: set[str]) -> list[dict]:
    return [item for item in listing if item["id"] not in seen]


def collect(board, seen: set[str]) -> list[str]:
    bodies = []
    for item in new_notices(board.list_notices(), seen):
        bodies.append(board.open_detail(item["id"]))
        seen.add(item["id"])
    return bodies
