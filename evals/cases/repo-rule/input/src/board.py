"""Board access. A check receives this protocol, never a client."""

from typing import Protocol


class Board(Protocol):
    def cards(self) -> list[dict]:
        """Return every card as {"id": str, "owner": str | None, "updated_at": float}."""
        ...
