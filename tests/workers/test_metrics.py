from unittest.mock import Mock

import pytest
from prometheus_client import REGISTRY
from tadoss import worker
from tadoss.adapters import queues
from tadoss.domain.validation import APIError

from tests.support.api import event


@pytest.mark.parametrize(
    "result,outcome",
    [
        (204, "success"),
        (503, "http_error"),
        (OSError("connection failed"), "transport_error"),
        (APIError(400, "destination rejected"), "rejected"),
        (RuntimeError("unexpected"), "internal_error"),
    ],
)
def test_webhook_attempt_metrics_include_failures(api, result, outcome):
    api.register()
    api.send("POST", api.events, event())
    labels = {"outcome": outcome}
    before = REGISTRY.get_sample_value("tadoss_webhook_delivery_total", labels) or 0
    timed = (
        REGISTRY.get_sample_value("tadoss_webhook_delivery_seconds_count", labels) or 0
    )
    sender = (
        Mock(side_effect=result)
        if isinstance(result, Exception)
        else Mock(return_value=result)
    )
    assert worker.run_once(api.app.state.pool, api.app.state.settings, sender)
    assert (
        REGISTRY.get_sample_value("tadoss_webhook_delivery_total", labels) == before + 1
    )
    assert (
        REGISTRY.get_sample_value("tadoss_webhook_delivery_seconds_count", labels)
        == timed + 1
    )


def test_skipped_subscription_does_not_count_as_delivery(api):
    webhook = api.register()
    api.send("POST", api.events, event())
    job = queues.claim(api.app.state.pool, api.app.state.settings)
    api.send("DELETE", "/service/webhooks/" + webhook["id"], status=204)
    before = {
        sample.name + str(sample.labels): sample.value
        for metric in REGISTRY.collect()
        if metric.name.startswith("tadoss_webhook_delivery")
        for sample in metric.samples
    }
    sender = Mock(return_value=204)
    worker.process_webhook(api.app.state.pool, job, api.app.state.settings, sender)
    sender.assert_not_called()
    after = {
        sample.name + str(sample.labels): sample.value
        for metric in REGISTRY.collect()
        if metric.name.startswith("tadoss_webhook_delivery")
        for sample in metric.samples
    }
    assert after == before
