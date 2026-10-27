"""Transport-safe release summaries; full authored handoffs remain in artifacts."""


def one_line_reason(value):
    return (
        " ".join(str(value or "").split())
        or "Stopped at local attempt budget without match"
    )[:900].rstrip()
