import httpx
import psycopg
import pytest

from scripts.performance import exercise, isolated_database, successful_run


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize(
    "status,rejected,allow,passed",
    [
        ("200", 0, False, True),
        ("200", 1, True, False),
        ("503", 0, False, False),
        ("503", 0, True, True),
        ("500", 0, True, False),
        ("ReadError", 0, True, False),
    ],
)
def test_benchmark_only_accepts_explicit_overload(status, rejected, allow, passed):
    result = {"requests": 1, "statuses": {status: 1}, "rejected_events": rejected}
    assert successful_run([result], allow) is passed
    assert not successful_run([], allow)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["event", "transport"])
async def test_benchmark_records_failures_inside_successful_http_responses(failure):
    def respond(request):
        if failure == "transport":
            raise httpx.ConnectError("test connection failure", request=request)
        return httpx.Response(200, json={"responses": [{"http_status_code": 400}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        report = await exercise(client, [("/events", [{}])], "write-1", 1, 0.01, -1)
    assert report["requests"] > 0
    assert sum(report["statuses"].values()) == report["requests"]
    if failure == "event":
        assert report["rejected_events"] == report["requests"]
    else:
        assert report["statuses"] == {"ConnectError": report["requests"]}


def test_benchmark_database_cleanup_on_failure(database, monkeypatch):
    monkeypatch.setenv("TADOSS_TEST_DATABASE", database)
    namespace = None
    with (
        pytest.raises(RuntimeError, match="benchmark failed"),
        isolated_database() as url,
    ):
        with psycopg.connect(url) as connection:
            namespace = connection.execute("SELECT current_schema()").fetchone()[0]
        assert namespace.startswith("perf_")
        raise RuntimeError("benchmark failed")
    with psycopg.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (namespace,)
            ).fetchone()
            is None
        )
