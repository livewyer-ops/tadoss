"""Bounded metric labels shared by API and worker processes."""

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "tadoss_http_requests_total", "HTTP responses", ["method", "route", "status"]
)
DURATION = Histogram(
    "tadoss_http_request_seconds", "HTTP request duration", ["method", "route"]
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
