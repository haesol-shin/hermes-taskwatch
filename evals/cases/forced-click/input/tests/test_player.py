import asyncio

import pytest
from fake_frame import FakeClock, FakeFrame
from src.player import (
    START_WAIT_SECONDS,
    PlaybackError,
    dismiss_consent,
    open_embed,
    replay,
    wait_until_playing,
)

URL = "https://video.example.invalid/embed"


def make(starts_on_its_own):
    frame = FakeFrame(starts_on_its_own)
    return frame, FakeClock(frame)


def test_consent_overlay_is_closed_when_it_is_shown():
    frame, _ = make(False)
    asyncio.run(frame.goto(URL))
    asyncio.run(dismiss_consent(frame))
    assert frame.overlay_shown is False


def test_open_embed_returns_for_an_embed_that_starts_on_its_own():
    frame, clock = make(True)
    asyncio.run(open_embed(frame, URL, clock))
    assert frame.urls == [URL]


def test_replay_returns_for_an_embed_that_starts_on_its_own():
    frame, clock = make(True)
    asyncio.run(open_embed(frame, URL, clock))
    asyncio.run(replay(frame, clock))
    assert frame.urls == [URL]


def test_waiting_for_a_video_that_never_plays_reports_it():
    frame, clock = make(False)
    asyncio.run(frame.goto(URL))
    with pytest.raises(PlaybackError, match="playback-not-started"):
        asyncio.run(wait_until_playing(frame, clock))
    assert clock.now >= START_WAIT_SECONDS
