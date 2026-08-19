from datetime import datetime, timezone


def utcnow() -> datetime:
    """
    Naive UTC, which is what every datetime column in DMM holds.

    The columns are TIMESTAMP WITHOUT TIME ZONE, so an aware value silently
    loses its offset on write and reads back naive. Writing naive UTC keeps
    every timestamp on one clock and makes stored values comparable to each
    other and to this function.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
