class PolicyError(Exception):
    status = 403
    code = "forbidden"

    def __init__(self, detail: str = "", code: str | None = None, status: int | None = None, extra=None):
        super().__init__(detail or self.code)
        self.detail = detail or "You are not allowed to do this."
        if code:
            self.code = code
        if status:
            self.status = status
        self.extra = extra or {}


class NotAuthenticated(PolicyError):
    status = 401
    code = "not_authenticated"


class Forbidden(PolicyError):
    status = 403
    code = "forbidden"


class DeadlinePassed(PolicyError):
    """Raised *before* body validation so a late submission is refused for the right reason."""

    status = 403
    code = "deadline_passed"


class WindowClosed(PolicyError):
    status = 403
    code = "window_closed"


class Conflict(PolicyError):
    status = 409
    code = "conflict"


class NotFound(PolicyError):
    status = 404
    code = "not_found"


class RateLimited(PolicyError):
    status = 429
    code = "rate_limited"


class Invalid(PolicyError):
    status = 422
    code = "validation_error"
