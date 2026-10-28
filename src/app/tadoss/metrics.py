"""Bounded metric labels shared by API and worker processes."""

import anyio
from prometheus_client import Counter, Gauge, Histogram
from psycopg_pool import ConnectionPool

REQUESTS = Counter(
    "tadoss_http_requests_total", "HTTP responses", ["method", "route", "status"]
)
DURATION = Histogram(
    "tadoss_http_request_seconds", "HTTP request duration", ["method", "route"]
)
INFLIGHT = Gauge("tadoss_http_inflight", "Requests occupying HTTP admission capacity")
DATABASE_ACTIVE = Gauge("tadoss_database_active", "Requests holding database admission")
DATABASE_WAITING = Gauge(
    "tadoss_database_waiting", "Requests waiting for database admission"
)
ADMISSION_WAIT = Histogram(
    "tadoss_database_admission_seconds",
    "Database admission wait, including timeouts and cancellations",
)
ADMISSION_REJECTIONS = Counter(
    "tadoss_admission_rejections_total", "Admission rejections", ["stage"]
)
POOL = Gauge(
    "tadoss_database_pool_connections", "Database pool connection counts", ["state"]
)
POOL_WAITING = Gauge(
    "tadoss_database_pool_waiting", "Requests waiting inside the connection pool"
)
LOOP_LAG = Histogram(
    "tadoss_event_loop_lag_seconds",
    "Event loop scheduling delay",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5),
)
WORK = Counter("tadoss_worker_jobs_total", "Processed worker jobs", ["kind", "outcome"])
QUEUE = Gauge("tadoss_queue_jobs", "Active background jobs", ["kind"])
AGE = Gauge("tadoss_queue_oldest_seconds", "Age of the oldest active job", ["kind"])
EVENTS = Counter(
    "tadoss_events_total", "Event results from committed batches", ["outcome"]
)
BATCH = Histogram(
    "tadoss_event_batch_size",
    "Number of events submitted per batch",
    buckets=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000),
)
WEBHOOK_ATTEMPTS = Counter(
    "tadoss_webhook_delivery_total", "Webhook delivery attempts", ["outcome"]
)
WEBHOOK_DURATION = Histogram(
    "tadoss_webhook_delivery_seconds", "Webhook delivery attempt duration", ["outcome"]
)


async def monitor_pool(pool: ConnectionPool) -> None:
    while True:
        stats = pool.get_stats()
        for label, key in (
            ("size", "pool_size"),
            ("available", "pool_available"),
            ("maximum", "pool_max"),
        ):
            POOL.labels(label).set(stats.get(key, 0))
        POOL_WAITING.set(stats.get("requests_waiting", 0))
        expected = anyio.current_time() + 1
        await anyio.sleep(1)
        LOOP_LAG.observe(max(0, anyio.current_time() - expected))
