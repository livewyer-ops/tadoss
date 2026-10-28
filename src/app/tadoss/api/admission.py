"""Bound request memory before authentication, body buffering and database waits."""

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from tadoss import metrics
from tadoss.domain.validation import APIError


class RequestAdmission:
    def __init__(self, app: ASGIApp, maximum: int) -> None:
        self.app = app
        self.maximum = maximum
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in {
            "/healthz",
            "/readyz",
            "/metrics",
        }:
            await self.app(scope, receive, send)
            return
        # No await between checking and reserving capacity on this event loop.
        if self.active >= self.maximum:
            metrics.ADMISSION_REJECTIONS.labels("http").inc()
            response = JSONResponse(
                APIError(503, "Request capacity exhausted; retry later").body,
                status_code=503,
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return
        self.active += 1
        metrics.INFLIGHT.inc()
        try:
            await self.app(scope, receive, send)
        finally:
            self.active -= 1
            metrics.INFLIGHT.dec()
