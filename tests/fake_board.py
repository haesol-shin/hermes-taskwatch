"""In-memory board. No network and no Hermes import."""

from dataclasses import dataclass

from taskwatch.protocol import BoardReadError, Card


@dataclass
class FakeBoard:
    items: tuple[Card, ...] = ()
    waits: tuple[str, ...] = ()
    error: str | None = None
    boom: bool = False

    def cards(self) -> tuple[Card, ...]:
        if self.boom:
            raise BoardReadError("board read failed")
        return self.items

    def card(self, card_id: str) -> Card | None:
        if self.boom:
            raise BoardReadError("board read failed")
        return next((item for item in self.items if item.id == card_id), None)

    def open_wait_ids(self, cards=None) -> tuple[str, ...]:
        return self.waits

    def read_error(self) -> str | None:
        return self.error
