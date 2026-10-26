# Spec

In a headed browser the embedded player sometimes does not start on its own. `open_embed` and `replay` must leave the video playing in that case, and report `playback-not-started` if nothing plays within `START_WAIT_SECONDS`.

The embed is at `https://video.example.invalid/embed`.
