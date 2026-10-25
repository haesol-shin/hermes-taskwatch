"""Save named attachments under a local root.

A fetch talks to https://files.example.invalid/attachments/<id>.
"""

from pathlib import Path


def index_files(root: Path) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found.setdefault(path.name, []).append(path)
    return found


def already_recorded(ledger: dict[str, str], attachment_id: str) -> bool:
    return ledger.get(attachment_id) == "done"


def save_attachment(path: Path, body: bytes) -> None:
    if path.is_file() and path.stat().st_size == len(body) and path.read_bytes() == body:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def adopt_local(root: Path, name: str, attachment_id: str, ledger: dict[str, str]) -> bool:
    for path in index_files(root).get(name, []):
        if path.stat().st_size > 0:
            ledger[attachment_id] = "done"
            return True
    return False


def fetch_attachment(
    root: Path,
    attachment_id: str,
    name: str,
    body: bytes,
    ledger: dict[str, str],
) -> None:
    if already_recorded(ledger, attachment_id):
        return
    if adopt_local(root, name, attachment_id, ledger):
        return
    save_attachment(root / name, body)
    ledger[attachment_id] = "done"
