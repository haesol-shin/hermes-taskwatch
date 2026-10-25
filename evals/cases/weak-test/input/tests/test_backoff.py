from src.backoff import CAP_SECONDS, retry_delay


def test_delay_doubles_from_two_seconds():
    assert [retry_delay(attempt) for attempt in range(3)] == [2.0, 4.0, 8.0]


def test_delay_never_exceeds_the_cap():
    for attempt in range(5):
        assert retry_delay(attempt) <= CAP_SECONDS
