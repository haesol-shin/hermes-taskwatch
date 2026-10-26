"""Download a body from https://files.example.invalid/blob."""

from collections.abc import Iterable

from src.validator import TransferError, Validator


def download(chunks: Iterable[bytes], declared_length: int | None) -> bytes:
    validator = Validator()
    body = bytearray()
    for chunk in chunks:
        validator.feed(chunk)
        body += chunk
    if declared_length is not None and len(body) != declared_length:
        raise TransferError("length-mismatch")
    validator.finish(declared_length)
    return bytes(body)
