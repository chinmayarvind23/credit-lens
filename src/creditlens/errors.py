"""Stable public errors avoid leaking provider payloads and sensitive document contents."""


class ServiceError(Exception):
    """Only curated codes and messages cross the HTTP boundary."""

    def __init__(self, code: str, message: str, status: int = 503) -> None:
        """Keep private provider exception text out of the user-visible error contract."""
        self.code = code
        self.message = message
        self.status = status
        super().__init__(code)
