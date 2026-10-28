import anyio
import httpx
import pytest
from prometheus_client import REGISTRY
from tadoss.adapters import postgres as store
from tadoss.api import create_app
from tadoss.settings import Settings

TRACK = "/tracks/20992138-abdb-4daf-84f3-a0e07dabb5ca"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def metric(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0


@pytest.mark.anyio
async def test_http_capacity_precedes_body_buffering_and_recovers_on_cancel(database):
    app = create_app(
        settings=Settings(
            database=database,
            api_token="test-secret",
            max_inflight_requests=1,
        )
    )
    entered = anyio.Event()

    async def blocked_body():
        yield b"{"
        entered.set()
        await anyio.sleep_forever()

    async def unread_body():
        raise AssertionError("Rejected request body must not be consumed")
        yield b""  # pragma: no cover

    before = metric("tadoss_admission_rejections_total", {"stage": "http"})
    async with app.router.lifespan_context(app):
        await anyio.to_thread.run_sync(store.initialize, app.state.pool)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            params={"access_token": "test-secret"},
        ) as client:

            async def hold():
                await client.put(
                    TRACK,
                    content=blocked_body(),
                    headers={"Content-Type": "application/json"},
                )

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(hold)
                with anyio.fail_after(2):
                    await entered.wait()
                assert metric("tadoss_http_inflight") == 1
                rejected = await client.put(TRACK, content=unread_body())
                assert rejected.status_code == 503
                assert rejected.headers["Retry-After"] == "1"
                assert rejected.json()["type"] == "urn:tadoss:error:503"
                assert (await client.get("/healthz")).status_code == 200
                assert (await client.get("/readyz")).status_code == 200
                assert (await client.get("/metrics")).status_code == 200
                tasks.cancel_scope.cancel()
            assert metric("tadoss_http_inflight") == 0
            assert (await client.get("/tracks")).status_code == 200
    assert metric("tadoss_admission_rejections_total", {"stage": "http"}) == before + 1


@pytest.mark.anyio
async def test_body_deadline_is_total_and_releases_capacity(database):
    app = create_app(
        settings=Settings(
            database=database,
            api_token="test-secret",
            max_inflight_requests=1,
            body_read_timeout=0.06,
        )
    )

    async def slow_body():
        for _ in range(100):
            yield b" "
            await anyio.sleep(0.02)
        yield b"{}"

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        with anyio.fail_after(1):
            response = await client.put(
                TRACK + "?access_token=test-secret",
                content=slow_body(),
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 408
        assert response.json()["type"] == "urn:tadoss:error:408"
        assert metric("tadoss_http_inflight") == 0
        assert app.state.database_slots.borrowed_tokens == 0
        assert (
            await client.get("/service?access_token=test-secret")
        ).status_code == 200
        assert (await client.put(TRACK, content=slow_body())).status_code == 401


@pytest.mark.anyio
async def test_admission_and_pool_metrics_include_timeout_and_recovery(database):
    app = create_app(
        settings=Settings(
            database=database,
            api_token="test-secret",
            pool_max_size=1,
            pool_timeout=0.05,
        )
    )
    before = metric("tadoss_admission_rejections_total", {"stage": "database"})
    waits = metric("tadoss_database_admission_seconds_count")
    lag = metric("tadoss_event_loop_lag_seconds_count")
    async with app.router.lifespan_context(app):
        await anyio.to_thread.run_sync(store.initialize, app.state.pool)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            params={"access_token": "test-secret"},
        ) as client:
            slots = app.state.database_slots
            held = object()
            await slots.acquire_on_behalf_of(held)
            try:
                assert (await client.get("/tracks")).status_code == 503
            finally:
                slots.release_on_behalf_of(held)
            assert metric("tadoss_database_waiting") == 0
            assert metric("tadoss_database_active") == 0
            assert (
                metric("tadoss_admission_rejections_total", {"stage": "database"})
                == before + 1
            )
            assert metric("tadoss_database_admission_seconds_count") == waits + 1
            assert (await client.get("/tracks")).status_code == 200
            with anyio.fail_after(3):
                while metric("tadoss_event_loop_lag_seconds_count") == lag:
                    await anyio.sleep(0.01)
            assert metric("tadoss_database_pool_connections", {"state": "maximum"}) == 1
            assert (
                metric("tadoss_database_pool_connections", {"state": "available"}) == 1
            )
            assert metric("tadoss_database_pool_waiting") == 0
            assert metric("tadoss_database_active") == 0


@pytest.mark.anyio
async def test_probes_do_not_wait_for_normal_request_threads(database):
    app = create_app(settings=Settings(database=database, api_token="test-secret"))
    async with app.router.lifespan_context(app):
        await anyio.to_thread.run_sync(store.initialize, app.state.pool)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            threads = anyio.to_thread.current_default_thread_limiter()
            previous = threads.total_tokens
            held = object()
            threads.total_tokens = 1
            await threads.acquire_on_behalf_of(held)
            try:
                with anyio.fail_after(1):
                    assert (await client.get("/healthz")).status_code == 200
                    assert (await client.get("/readyz")).status_code == 200
                assert app.state.readiness_slots.borrowed_tokens == 0
                await app.state.readiness_slots.acquire_on_behalf_of(held)
                try:
                    assert (await client.get("/readyz")).status_code == 503
                finally:
                    app.state.readiness_slots.release_on_behalf_of(held)
            finally:
                threads.release_on_behalf_of(held)
                threads.total_tokens = previous
