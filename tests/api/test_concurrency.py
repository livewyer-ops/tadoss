from threading import Event

import anyio
import httpx
import pytest
from tadoss.adapters import postgres as store
from tadoss.api import create_app
from tadoss.settings import Settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("pool_size", [1, 10])
async def test_concurrent_requests_do_not_starve_database_holders(database, pool_size):
    settings = Settings(
        database=database,
        api_token="test-secret",
        pool_min_size=pool_size,
        pool_max_size=pool_size,
        pool_timeout=2,
    )
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        await anyio.to_thread.run_sync(store.initialize, app.state.pool)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:

            async def read():
                response = await client.get("/tracks?access_token=test-secret")
                assert response.status_code == 200, response.text

            async with anyio.create_task_group() as tasks:
                for _ in range(100):
                    tasks.start_soon(read)
            assert app.state.database_slots.borrowed_tokens == 0
            assert app.state.pool.get_stats()["pool_available"] == pool_size
            assert (await client.get("/readyz")).status_code == 200


@pytest.mark.anyio
async def test_admission_timeout_and_cancelled_waiter_release_capacity(database):
    app = create_app(
        settings=Settings(
            database=database,
            api_token="test-secret",
            pool_max_size=1,
            pool_timeout=0.1,
        )
    )
    async with app.router.lifespan_context(app):
        await anyio.to_thread.run_sync(store.initialize, app.state.pool)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            slots = app.state.database_slots
            held = object()
            await slots.acquire_on_behalf_of(held)
            try:
                response = await client.get("/tracks?access_token=test-secret")
                assert response.status_code == 503
                assert response.headers["Retry-After"] == "1"
                # Rejected authentication still precedes admission.
                assert (await client.get("/tracks")).status_code == 401
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(client.get, "/tracks?access_token=test-secret")
                    with anyio.fail_after(1):
                        while slots.statistics().tasks_waiting != 1:
                            await anyio.sleep(0.001)
                    tasks.cancel_scope.cancel()
                assert slots.statistics().tasks_waiting == 0
                assert slots.borrowed_tokens == 1
            finally:
                slots.release_on_behalf_of(held)
            assert (
                await client.get("/tracks?access_token=test-secret")
            ).status_code == 200
            assert slots.borrowed_tokens == 0


@pytest.mark.anyio
async def test_cancelled_active_request_releases_connection_and_admission(
    database, monkeypatch
):
    app = create_app(
        settings=Settings(database=database, api_token="test-secret", pool_max_size=1)
    )
    entered, release = Event(), Event()
    original = store.listing

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release the running query"
        return original(*args, **kwargs)

    async with app.router.lifespan_context(app):
        await anyio.to_thread.run_sync(store.initialize, app.state.pool)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with monkeypatch.context() as patch:
                patch.setattr(store, "listing", blocked)
                try:
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(client.get, "/tracks?access_token=test-secret")
                        assert await anyio.to_thread.run_sync(entered.wait, 5)
                        tasks.cancel_scope.cancel()
                        release.set()
                finally:
                    release.set()
            assert app.state.database_slots.borrowed_tokens == 0
            assert app.state.pool.get_stats()["pool_available"] == 1
            assert (
                await client.get("/tracks?access_token=test-secret")
            ).status_code == 200
