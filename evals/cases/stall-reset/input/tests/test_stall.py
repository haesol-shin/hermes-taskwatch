from src.stall import STALL_AFTER_SECONDS, is_stalled, note_progress


def test_advancing_position_updates_state():
    previous, last_progress_at = note_progress(12.0, 10.0, 100.0, 130.0, False)
    assert previous == 12.0
    assert last_progress_at == 130.0


def test_paused_playback_keeps_state():
    assert note_progress(10.0, 10.0, 100.0, 400.0, True) == (10.0, 100.0)


def test_paused_playback_is_not_a_stall():
    assert is_stalled(0.0, 1000.0, True) is False


def test_long_silence_is_a_stall():
    assert is_stalled(0.0, STALL_AFTER_SECONDS + 1, False) is True


def test_recent_progress_is_not_a_stall():
    assert is_stalled(100.0, 100.0 + STALL_AFTER_SECONDS, False) is False
