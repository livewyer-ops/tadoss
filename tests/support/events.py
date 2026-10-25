from tadoss.domain.types import Document
from tadoss.domain.validation import time_range


def event_key(
    event: Document, reverse: bool = False
) -> tuple[int | float, bool, int | float, bool, str]:
    timerange = time_range(event["timerange"])
    start = (
        timerange.start.to_nanosec() if timerange.start is not None else float("-inf"),
        not timerange.includes_start(),
    )
    end = (
        timerange.end.to_nanosec() if timerange.end is not None else float("inf"),
        timerange.includes_end(),
    )
    return (*end, *start, event["id"]) if reverse else (*start, *end, event["id"])
