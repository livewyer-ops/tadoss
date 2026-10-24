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
