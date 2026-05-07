"""Custom exceptions raised by the Jolpica client.

The worker catches these to decide whether to retry, alert, or move on.
"""


class JolpicaError(Exception):
    """Base class for all Jolpica client errors."""


class JolpicaTransientError(JolpicaError):
    """Network blip, timeout, or 5xx response. Worker should retry later."""


class JolpicaRateLimitError(JolpicaTransientError):
    """Rate-limited by Jolpica (HTTP 429)."""


class JolpicaParseError(JolpicaError):
    """Response was 200 but the structure didn't match what we expected.

    Treat as a hard failure — likely an upstream schema change requiring
    manual investigation rather than a retry.
    """


class JolpicaNotFoundError(JolpicaError):
    """Endpoint returned an empty result (e.g. round results not yet published)."""
