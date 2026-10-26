"""systemd user units. ExecStart is an argument list, not a shell string."""

from pathlib import Path

_UNSAFE = set(" \t\n\"'\\$`;|&<>")


class UnitError(ValueError):
    """The unit would not be a plain argument list."""


def _plain(path: str, label: str) -> str:
    if not path or any(char in _UNSAFE for char in path) or not Path(path).is_absolute():
        raise UnitError(f"{label} must be an absolute path without shell characters")
    return path


def service_text(hermes_exe: str, hermes_home: str, profile: str) -> str:
    exe = _plain(hermes_exe, "hermes")
    home = _plain(hermes_home, "HERMES_HOME")
    profile_home(home, profile)
    return (
        "[Unit]\n"
        "Description=Scan the taskwatch board once\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=HERMES_HOME={home}\n"
        f"ExecStart={exe} -p {profile} taskwatch scan\n"
    )


def timer_text() -> str:
    return (
        "[Unit]\n"
        "Description=Scan the taskwatch board every five minutes\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=5min\n"
        "OnUnitActiveSec=5min\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def profile_home(root: str, profile: str) -> str:
    if profile == "default":
        return root
    if not profile or any(char in _UNSAFE or char == "/" for char in profile):
        raise UnitError("profile must be a plain name")
    return str(Path(root) / "profiles" / profile)
