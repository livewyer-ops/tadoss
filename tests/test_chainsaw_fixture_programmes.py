import runpy
import traceback
from http.client import BadStatusLine
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError

import httpx
import psycopg
import pytest
from tadoss.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "operator/test/chainsaw/fixtures/api_check.py"
MARKER = "fixture-credential-marker"


@pytest.fixture
def fixture_environment(monkeypatch):
    monkeypatch.setenv("TADOSS_TOKEN", MARKER)
    monkeypatch.setenv("MODE", "verify")
    monkeypatch.setenv("ROLLOUT_TIMEOUT", "0")


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError(
            "https://example.test/?access_token=" + MARKER, 401, MARKER, {}, None
        ),
        HTTPError(
            "https://example.test/?access_token=" + MARKER, 503, MARKER, {}, None
        ),
        URLError(MARKER),
        BadStatusLine(MARKER),
        ValueError(MARKER),
    ],
)
def test_fixture_process_redacts_http_failures(
    monkeypatch, fixture_environment, failure
):
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=failure))
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(FIXTURE), run_name="__main__")
    output = "".join(traceback.format_exception(error.value))
    assert MARKER not in output
    assert "HTTP operation failed" in output
    if isinstance(failure, HTTPError):
        assert str(failure.code) in str(error.value)


@pytest.mark.parametrize("code,retryable", [(401, False), (404, False), (503, True)])
def test_fixture_keeps_retry_classification(
    monkeypatch, fixture_environment, code, retryable
):
    retry = runpy.run_path(str(FIXTURE))["retry"]
    clock = SimpleNamespace(monotonic=lambda: 0, sleep=Mock())
    retry.__globals__.update(time=clock, deadline=10)
    failure = HTTPError("https://example.test", code, "unavailable", {}, None)
    action = Mock(side_effect=[failure, "ready"])
    if retryable:
        assert retry(action) == "ready"
        assert action.call_count == 2
        clock.sleep.assert_called_once_with(2)
    else:
        with pytest.raises(HTTPError):
            retry(action)
        action.assert_called_once()
        clock.sleep.assert_not_called()


def test_fixture_retry_stops_at_the_rollout_deadline(fixture_environment):
    retry = runpy.run_path(str(FIXTURE))["retry"]
    clock = SimpleNamespace(monotonic=Mock(side_effect=[1, 3]), sleep=Mock())
    retry.__globals__.update(time=clock, deadline=2)
    action = Mock(side_effect=URLError("unavailable"))
    with pytest.raises(URLError):
        retry(action)
    assert action.call_count == 2
    clock.sleep.assert_called_once_with(2)


def test_deployed_process_redacts_transport_failures(monkeypatch):
    monkeypatch.setenv("TADOSS_API_TOKEN", MARKER)
    monkeypatch.setattr(
        Settings,
        "load",
        lambda: Settings(database="host=localhost sslmode=verify-full"),
    )
    database = MagicMock()
    database.__enter__.return_value.execute.return_value.fetchone.return_value = [True]
    monkeypatch.setattr(
        psycopg, "connect", Mock(side_effect=[database, psycopg.OperationalError()])
    )
    monkeypatch.setattr(
        "socket.getaddrinfo",
        Mock(return_value=[(None, None, None, None, ("127.0.0.1", 5432))]),
    )
    client = MagicMock()
    client.__enter__.return_value.get.side_effect = [
        httpx.Response(200),
        httpx.Response(401),
    ]
    client.__enter__.return_value.request.side_effect = httpx.ReadError(MARKER)
    monkeypatch.setattr(httpx, "Client", Mock(return_value=client))
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / "tests/deployed/checks.py"), run_name="__main__")
    assert MARKER not in "".join(traceback.format_exception(error.value))
    assert str(error.value) == "HTTP operation failed"
