class FPLError(Exception):
    """Base class for anything that went wrong talking to FPL."""


class NotFound(FPLError):
    """The API returned 404 — usually a bad league id, entry id, or a gameweek
    whose deadline has not passed yet."""


class UpstreamUnavailable(FPLError):
    """FPL is down, rate-limiting us, or Cloudflare is unhappy. Serve cache."""


class SoftBlocked(UpstreamUnavailable):
    """403 on a public endpoint — treat as an IP block and back off hard."""
