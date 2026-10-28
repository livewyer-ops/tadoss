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
async def test_scrape_token_has_no_api_access_and_survives_pool_thread_exhaustion(
    database,
):
    app = create_app(
        settings=Settings(
            database=database,
            api_token="api-secret",
            metrics_token="scrape-secret",
            pool_max_size=1,
        )
    )
    async with app.router.lifespan_context(app):
        await anyio.to_thread.run_sync(store.initialize, app.state.pool)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"Authorization": "Bearer scrape-secret"},
        ) as client:
            assert (await client.get("/tracks")).status_code == 401
            assert (
                await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            threads = anyio.to_thread.current_default_thread_limiter()
            previous, held = threads.total_tokens, object()
            threads.total_tokens = 1
            await threads.acquire_on_behalf_of(held)
            connection = app.state.pool.getconn()
            try:
                with anyio.fail_after(2):
                    assert (await client.get("/readyz")).status_code == 200
                    assert (await client.get("/metrics")).status_code == 200
                await app.state.metrics_slots.acquire_on_behalf_of(held)
                try:
                    assert (await client.get("/metrics")).status_code == 503
                finally:
                    app.state.metrics_slots.release_on_behalf_of(held)
                assert (await client.get("/metrics")).status_code == 200
            finally:
                app.state.pool.putconn(connection)
                threads.release_on_behalf_of(held)
                threads.total_tokens = previous
