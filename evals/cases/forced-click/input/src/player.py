"""Start playback in an embedded player at https://video.example.invalid/embed."""

START_WAIT_SECONDS = 30.0
PLAY_BUTTON = ".play-button"
CONSENT_OVERLAY = ".consent-overlay"


class PlaybackError(Exception):
    pass


async def dismiss_consent(frame) -> None:
    overlay = frame.locator(CONSENT_OVERLAY)
    if await overlay.count() and await overlay.is_visible():
        await overlay.locator("button.accept").click(timeout=2000)


async def press_play(frame) -> None:
    await frame.locator(PLAY_BUTTON).click(force=True)


async def wait_until_playing(frame, clock) -> None:
    deadline = clock.monotonic() + START_WAIT_SECONDS
    while clock.monotonic() < deadline:
        if await frame.evaluate("document.querySelector('video').currentTime > 0"):
            return
        await clock.sleep(0.5)
    raise PlaybackError("playback-not-started")


async def open_embed(frame, url: str, clock) -> None:
    await frame.goto(url)
    await dismiss_consent(frame)
    await press_play(frame)
    await wait_until_playing(frame, clock)


async def replay(frame, clock) -> None:
    await frame.reload()
    await press_play(frame)
    await wait_until_playing(frame, clock)
