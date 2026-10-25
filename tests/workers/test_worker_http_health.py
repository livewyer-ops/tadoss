from __future__ import annotations

import urllib.error
import urllib.request

import pytest
from tadoss.settings import Settings
from tadoss.worker_health import WorkerHealthState, start_worker_http_server


def test_worker_http_server_tracks_readiness_without_backend_calls() -> None:
    state = WorkerHealthState(600)
    server = start_worker_http_server(_settings(), state)
    assert server is not None
    try:
        assert _status(server.port, "/healthz") == 200
        assert _status(server.port, "/readyz") == 503

        state.mark_poll_succeeded()
        assert _status(server.port, "/readyz") == 200

        state.mark_poll_failed()
        assert _status(server.port, "/healthz") == 200
        assert _status(server.port, "/readyz") == 503

        status, body = _response(server.port, "/metrics")
        assert status == 200
        assert b"python_info" in body
    finally:
        server.close()


def test_worker_health_becomes_unhealthy_when_progress_is_stale() -> None:
    now = [100.0]
    state = WorkerHealthState(30, clock=lambda: now[0])
    state.mark_poll_succeeded()

    assert state.is_live() is True
    assert state.is_ready() is True

    now[0] += 31

    assert state.is_live() is False
    assert state.is_ready() is False


def test_worker_health_stops_readiness_during_shutdown() -> None:
    state = WorkerHealthState(600)
    state.mark_poll_succeeded()

    state.mark_stopping()

    assert state.is_live() is False
    assert state.is_ready() is False


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        worker_health_port=0,
    )


def _status(port: int, path: str) -> int:
    return _response(port, path)[0]


def _response(port: int, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}",
            timeout=5,
        ) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.mark.parametrize(
    "path,status",
    [("/healthz", 200), ("/readyz?probe=1", 503), ("/missing", 404), ("/metrics", 200)],
)
def test_worker_http_head_matches_get_without_a_body(path, status):
    server = start_worker_http_server(_settings(), WorkerHealthState(600))
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}{path}", method="HEAD"
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)  # noqa: S310
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            assert response.status == status
            assert response.read() == b""
            assert int(response.headers["Content-Length"]) > 0
        assert _status(server.port, path) == status
    finally:
        server.close()
    assert not server.thread.is_alive()


def test_worker_http_bind_failure_is_fatal():
    from dataclasses import replace

    server = start_worker_http_server(_settings(), WorkerHealthState(600))
    try:
        with pytest.raises(OSError):
            start_worker_http_server(
                replace(_settings(), worker_health_port=server.port),
                WorkerHealthState(600),
            )
    finally:
        server.close()
