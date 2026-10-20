"""One validity window for CF cards and all consumers of their current URL."""

from datetime import datetime

VERIFICATION_TTL_SECONDS = 60
CLOCK_ROUNDING_TOLERANCE_SECONDS = 0.001


def verification_is_fresh(verified_at: str | None, *, now: float) -> bool:
    try:
        age = now - datetime.fromisoformat(verified_at).timestamp()
    except (TypeError, ValueError):
        return False
    # Stored ISO timestamps round to microseconds; frozen clocks can be earlier
    # by a fraction of a microsecond, without representing a future observation.
    return -CLOCK_ROUNDING_TOLERANCE_SECONDS <= age < VERIFICATION_TTL_SECONDS
