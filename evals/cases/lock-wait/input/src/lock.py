"""Single-operation lock for a command-line tool."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager

BUSY_EXIT_STATUS = 75


class BusyError(Exception):
    exit_status = BUSY_EXIT_STATUS


@contextmanager
def operation_lock(path: str) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            # Wait for the other operation so a scheduled run does not need a retry.
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BlockingIOError:
            raise BusyError("another operation holds the lock") from None
        yield
    finally:
        os.close(fd)
