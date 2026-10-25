"""Logging and optional OpenTelemetry configuration."""

import logging
import logging.config
import os
from typing import Any

from fastapi import FastAPI
from psycopg import Connection
from starlette.types import Scope

from . import __version__

logger = logging.getLogger("tadoss")


def configure_logging(level: str | None = None, encoding: str | None = None) -> None:
    """Send every logger to stdout with the request id, as JSON or text."""
    level = (level or os.getenv("TADOSS_LOG_LEVEL", "info")).upper()
    encoding = encoding or os.getenv("TADOSS_LOG_FORMAT", "json")
    if encoding == "json":
        formatter = {
            "()": "pythonjsonlogger.json.JsonFormatter",
            "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s %(correlation_id)s",
            "rename_fields": {
                "asctime": "time",
                "levelname": "level",
                "correlation_id": "request_id",
            },
        }
    else:
        formatter = {
            "format": "%(asctime)s %(levelname)s %(name)s"
            " [%(correlation_id)s] %(message)s"
        }
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "request_id": {
                    "()": "asgi_correlation_id.CorrelationIdFilter",
                    "default_value": "-",
                }
            },
            "formatters": {"default": formatter},
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "filters": ["request_id"],
                    "formatter": "default",
                }
            },
            "root": {"level": level, "handlers": ["stdout"]},
            # uvicorn's access log stays off: the API logs without query strings.
            "loggers": {
                "uvicorn.error": {"handlers": [], "propagate": True},
                "uvicorn.access": {"handlers": [], "propagate": False},
            },
        }
    )


def instrument(app: FastAPI) -> bool:
    """Export traces and metrics when OTEL_EXPORTER_OTLP_ENDPOINT is set."""
    if getattr(app.state, "telemetry", None):
        return True
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the otel extra is not installed"
        )
        return False
    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "tadoss"),
            "service.version": __version__,
        }
    )
    tracer = TracerProvider(resource=resource)
    tracer.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(timeout=2), export_timeout_millis=2000)
    )
    meter = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(timeout=2))],
    )
    app.state.telemetry = (tracer, meter)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer,
        meter_provider=meter,
        server_request_hook=redact_query,
        excluded_urls="healthz,readyz,metrics",
    )
    return True


def redact_query(span: Any, scope: Scope) -> None:
    """Keep query strings, which may carry a bearer token, out of server spans."""
    attributes = getattr(span, "attributes", None) or {}
    path = scope.get("path", "")
    for key in ("http.url", "url.full"):
        if key in attributes:
            span.set_attribute(key, path)
    if "url.query" in attributes:
        span.set_attribute("url.query", "")


def instrument_connection(connection: Connection[Any], app: FastAPI) -> None:
    providers = getattr(app.state, "telemetry", None)
    if providers:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

        PsycopgInstrumentor.instrument_connection(
            connection, tracer_provider=providers[0]
        )


def shutdown(app: FastAPI) -> None:
    providers = getattr(app.state, "telemetry", None)
    if providers:
        providers[0].shutdown()
        providers[1].shutdown(timeout_millis=2000)
        app.state.telemetry = None
