"""Check a downloaded body against its limits."""


class TransferError(Exception):
    pass


MAX_BYTES = 1_000_000


class Validator:
    def __init__(self) -> None:
        self.size = 0

    def feed(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > MAX_BYTES:
            raise TransferError("too-large")

    def finish(self, declared_length: int | None) -> None:
        if not self.size or (declared_length is not None and self.size > declared_length):
            raise TransferError("bad-length")
