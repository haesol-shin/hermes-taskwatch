"""systemd user units."""


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
