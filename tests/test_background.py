import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

from tadoss import worker
from tadoss.adapters import postgres as store
from tadoss.application import background
from tadoss.auth import Identity
from tadoss.domain.validation import SPEC, contract_schema, validate

from tests import test_api


class BackgroundTests(unittest.TestCase):
    setUp = test_api.StoreTests.setUp
    send = test_api.StoreTests.send

    def register(self, **extra):
        body = {
            "url": "https://receiver.example/events",
            "events": list(SPEC["webhooks"]),
            "api_key_name": "X-Token",
            "api_key_value": "secret",
            **extra,
        }
        return self.send("POST", "/service/webhooks", body, status=201).json()

    def test_webhook_lifecycle_delivery_and_secret(self):
        webhook = self.register(tags={"kind": ["test"]})
        validate(webhook, contract_schema("webhook-get"))
        self.assertNotIn("api_key_value", webhook)
        self.send("POST", self.events, test_api.event())
        captured = []

        def receiver(url, body, header, secret, settings):
            captured.append((body, header, secret))
            return 204

        self.assertTrue(
            worker.run_once(self.app.state.pool, self.app.state.settings, receiver)
        )
        self.assertEqual(captured[0][0]["event_type"], "tracks/events_added")
        self.assertEqual(captured[0][1:], ("X-Token", "secret"))
        self.assertEqual(
            self.send("GET", "/service/webhooks/" + webhook["id"]).json()["status"],
            "started",
        )
        listed = self.send(
            "GET", "/service/webhooks", params={"tag.kind": "test"}
        ).json()
        self.assertEqual(len(listed), 1)
        self.send(
            "PUT",
            "/service/webhooks/" + webhook["id"],
            {**webhook, "status": "disabled"},
            status=204,
        )
        self.send("POST", self.events, test_api.event())
        self.assertFalse(
            worker.run_once(self.app.state.pool, self.app.state.settings, receiver)
        )
        self.send("DELETE", "/service/webhooks/" + webhook["id"], status=204)
        self.send("GET", "/service/webhooks/" + webhook["id"], status=404)

    def test_webhook_owner_and_stale_revision(self):
        webhook = self.register()
        with store.transaction(self.app.state.pool) as db:
            with self.assertRaises(Exception) as error:
                background.webhook_get(db, webhook["id"], Identity("other"))
            self.assertEqual(error.exception.status, 404)
        self.send("POST", self.events, test_api.event())
        job = background.claim(self.app.state.pool, self.app.state.settings)
        self.send(
            "PUT",
            "/service/webhooks/" + webhook["id"],
            {**webhook, "status": "disabled"},
            status=204,
        )
        with patch("tadoss.worker.deliver") as deliver:
            worker.process_webhook(
                self.app.state.pool, job, self.app.state.settings, deliver
            )
            deliver.assert_not_called()

    def test_async_deletion_preserves_later_mutations_and_recovers_claim(self):
        self.app.state.settings = replace(self.app.state.settings, delete_batch_size=1)
        identifiers = [str(uuid4()) for _ in range(3)]
        self.send(
            "POST", self.events, [test_api.event(id=value) for value in identifiers]
        )
        response = self.send("DELETE", self.events, status=202)
        request = response.json()
        validate(request, contract_schema("track-delete-request"))
        self.send(
            "POST",
            self.events,
            test_api.event(id=identifiers[0], payload={"text": "new"}),
        )
        stale = background.claim(self.app.state.pool, self.app.state.settings)
        with store.transaction(self.app.state.pool, write=True) as db:
            db.execute(
                "UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s",
                (stale[0],),
            )
        job = background.claim(self.app.state.pool, self.app.state.settings)
        background.process_delete(self.app.state.pool, stale, self.app.state.settings)
        self.assertEqual(len(self.send("GET", self.events).json()), 3)
        background.process_delete(self.app.state.pool, job, self.app.state.settings)
        while worker.run_once(self.app.state.pool, self.app.state.settings):
            pass
        self.assertEqual(
            [x["id"] for x in self.send("GET", self.events).json()], identifiers[:1]
        )
        self.assertEqual(
            self.send("GET", response.headers["Location"]).json()["status"], "done"
        )

    def test_whole_track_deletion_reserves_track(self):
        self.app.state.settings = replace(self.app.state.settings, delete_batch_size=1)
        self.send("POST", self.events, [test_api.event(), test_api.event()])
        response = self.send("DELETE", f"/tracks/{test_api.TRACK_ID}", status=202)
        self.send("POST", self.events, test_api.event(), status=409)
        while worker.run_once(self.app.state.pool, self.app.state.settings):
            pass
        self.send("GET", f"/tracks/{test_api.TRACK_ID}", status=404)
        self.assertEqual(
            self.send("GET", response.headers["Location"]).json()["status"], "done"
        )

    def test_rejected_mutations_do_not_enqueue(self):
        self.register()
        self.send("POST", self.events, test_api.event(payload={"text": 4}))
        self.assertFalse(worker.run_once(self.app.state.pool, self.app.state.settings))
        self.send(
            "POST",
            "/service/webhooks",
            {"url": "http://169.254.169.254/", "events": []},
            status=400,
        )

    def test_deletion_paging_expiry_and_progress(self):
        self.app.state.settings = replace(self.app.state.settings, delete_batch_size=1)
        self.send(
            "POST",
            self.events,
            [test_api.event(timerange=f"[{n}:0_{n + 1}:0)") for n in range(3)],
        )
        requests = [
            self.send("DELETE", self.events, status=202).json() for _ in range(3)
        ]
        latest = requests[-1]["id"]
        self.assertEqual(
            self.send("GET", "/track-delete-requests", params={"limit": 1}).json()[0][
                "id"
            ],
            latest,
        )
        with store.transaction(self.app.state.pool, write=True) as db:
            db.execute(
                "UPDATE jobs SET expiry=now()+interval '1 day' WHERE id=%s", (latest,)
            )
        seen = []
        params = {"sort_by": "expiry", "limit": 1}
        while True:
            response = self.send("GET", "/track-delete-requests", params=params)
            seen.extend(x["id"] for x in response.json())
            if "X-Paging-NextKey" not in response.headers:
                break
            params["page"] = response.headers["X-Paging-NextKey"]
        self.assertEqual(seen[0], latest)
        self.assertEqual(set(seen), {x["id"] for x in requests})
        worker.run_once(self.app.state.pool, self.app.state.settings)
        progress = self.send(
            "GET", "/track-delete-requests/" + requests[0]["id"]
        ).json()
        self.assertEqual(progress["status"], "started")
        self.assertNotEqual(progress["timerange_remaining"], "_")

    def test_expired_delete_claim_rolls_back_mutation(self):
        self.app.state.settings = replace(self.app.state.settings, delete_batch_size=1)
        self.send("POST", self.events, [test_api.event(), test_api.event()])
        self.send("DELETE", self.events, status=202)
        job = background.claim(self.app.state.pool, self.app.state.settings)
        with (
            patch("tadoss.application.background.finish", return_value=False),
            self.assertRaises(RuntimeError),
        ):
            background.process_delete(self.app.state.pool, job, self.app.state.settings)
        self.assertEqual(len(self.send("GET", self.events).json()), 2)

    def test_all_notification_types_and_retry(self):
        self.register()
        schema, track, identifier = str(uuid4()), str(uuid4()), str(uuid4())
        self.send(
            "PUT",
            "/schemas/" + schema,
            {"id": schema, "schema": {"type": "object"}},
            status=201,
        )
        self.send("PUT", "/schemas/" + schema + "/label", "Updated", status=204)
        self.send(
            "PUT",
            "/tracks/" + track,
            {
                "id": track,
                "schema_id": schema,
                "timeline_reference": {"entity_id": "urn:qualification"},
                "editable_events": True,
            },
            status=201,
        )
        self.send("PUT", "/tracks/" + track + "/label", "Updated", status=204)
        self.send("POST", "/tracks/" + track + "/events", test_api.event(id=identifier))
        self.send(
            "POST",
            "/tracks/" + track + "/events",
            test_api.event(id=identifier, payload={"text": "changed"}),
        )
        self.send("DELETE", "/tracks/" + track + "/events", status=204)
        self.send("DELETE", "/tracks/" + track, status=204)
        self.send("DELETE", "/schemas/" + schema, status=204)
        received = []

        def receive(_url, body, _header, _secret, _settings):
            received.append(body["event_type"])
            return 204

        while worker.run_once(self.app.state.pool, self.app.state.settings, receive):
            pass
        self.assertEqual(set(received), set(SPEC["webhooks"]))
        self.send("POST", self.events, test_api.event())
        self.assertTrue(
            worker.run_once(
                self.app.state.pool, self.app.state.settings, lambda *_: 503
            )
        )
        with store.transaction(self.app.state.pool, write=True) as db:
            db.execute("UPDATE jobs SET available=now() WHERE status='created'")
        self.assertTrue(
            worker.run_once(self.app.state.pool, self.app.state.settings, receive)
        )
        self.assertEqual(received[-1], "tracks/events_added")

    def test_delete_failure_budget_counts_consecutive_failed_batches(self):
        settings = replace(
            self.app.state.settings,
            delete_batch_size=1,
            delete_max_attempts=2,
            webhook_max_attempts=1,
        )
        self.app.state.settings = settings
        self.send("POST", self.events, [test_api.event() for _ in range(8)])
        request = self.send("DELETE", self.events, status=202).json()
        pool = self.app.state.pool
        for _ in range(5):
            worker.run_once(pool, settings)
        for _ in range(2):
            with patch.object(
                background, "process_delete", side_effect=RuntimeError("sensitive")
            ):
                worker.run_once(pool, settings)
            with store.transaction(pool, write=True) as db:
                row = db.execute(
                    "SELECT status,attempts FROM jobs WHERE id=%s", (request["id"],)
                ).fetchone()
                self.assertEqual(row, ("created", 1))
                db.execute(
                    "UPDATE jobs SET available=now() WHERE id=%s", (request["id"],)
                )
            worker.run_once(pool, settings)
        with patch.object(
            background, "process_delete", side_effect=RuntimeError("sensitive")
        ):
            for _ in range(2):
                worker.run_once(pool, settings)
                with store.transaction(pool, write=True) as db:
                    db.execute(
                        "UPDATE jobs SET available=now() WHERE id=%s", (request["id"],)
                    )
        self.assertEqual(
            self.send("GET", "/track-delete-requests/" + request["id"]).json()[
                "status"
            ],
            "error",
        )

    def test_webhook_failures_finish_subscription_and_allow_get_put_reenable(self):
        import http.client

        settings = replace(self.app.state.settings, webhook_max_attempts=1)
        for failure in (
            http.client.BadStatusLine("secret bytes"),
            RuntimeError("secret bytes"),
        ):
            with self.subTest(failure=type(failure).__name__):
                webhook = self.register()
                path = "/service/webhooks/" + webhook["id"]
                self.send("POST", self.events, test_api.event())
                with self.assertLogs("tadoss.worker") as logs:
                    worker.run_once(
                        self.app.state.pool, settings, Mock(side_effect=failure)
                    )
                self.assertNotIn("secret bytes", str(logs.output))
                current = self.send("GET", path).json()
                self.assertEqual(current["status"], "error")
                self.assertNotIn("secret bytes", str(current))
                self.send("PUT", path, {**current, "status": "created"}, status=204)
                self.assertNotIn("error", self.send("GET", path).json())
                self.send("DELETE", path, status=204)

    def test_webhook_revision_preserves_metadata_credentials_and_membership_order(self):
        webhook = self.register(track_ids=[test_api.TRACK_ID, str(uuid4())])
        path = "/service/webhooks/" + webhook["id"]
        self.send("POST", self.events, test_api.event())
        changed = {
            **webhook,
            "tags": {"label": ["new"]},
            "api_key_name": "X-New",
            "api_key_value": "rotated",
            "events": list(reversed(webhook["events"])),
            "track_ids": list(reversed(webhook["track_ids"])),
        }
        self.send("PUT", path, changed, status=204)
        sender = Mock(return_value=204)
        worker.run_once(self.app.state.pool, self.app.state.settings, sender)
        self.assertEqual(sender.call_args.args[2:4], ("X-New", "rotated"))
        self.assertEqual(self.send("GET", path).json()["tags"], {"label": ["new"]})
        self.send("POST", self.events, test_api.event())
        self.send("PUT", path, changed, status=204)  # started -> created stays active
        worker.run_once(self.app.state.pool, self.app.state.settings, sender)
        self.assertEqual(sender.call_count, 2)
        self.send("POST", self.events, test_api.event())
        self.send("PUT", path, {**changed, "status": "disabled"}, status=204)
        self.send("PUT", path, changed, status=204)
        worker.run_once(self.app.state.pool, self.app.state.settings, sender)
        self.assertEqual(sender.call_count, 2)

    def test_webhook_completion_preserves_edits_and_resets_rotated_budget(
        self,
    ):
        settings = replace(self.app.state.settings, webhook_max_attempts=1)
        webhook = self.register()
        path = "/service/webhooks/" + webhook["id"]
        self.send("POST", self.events, test_api.event())

        def rotate(*_args):
            self.send(
                "PUT",
                path,
                {
                    **webhook,
                    "tags": {"changed": ["yes"]},
                    "api_key_name": "X-New",
                    "api_key_value": "new-secret",
                },
                status=204,
            )
            return 401

        worker.run_once(self.app.state.pool, settings, rotate)
        current = self.send("GET", path).json()
        self.assertEqual(current["status"], "started")
        self.assertEqual(current["tags"], {"changed": ["yes"]})
        self.assertEqual(current["api_key_name"], "X-New")
        with store.transaction(self.app.state.pool, write=True) as db:
            self.assertEqual(
                db.execute("SELECT status,attempts FROM jobs").fetchone(),
                ("created", 0),
            )
            db.execute("UPDATE jobs SET available=now()")
        sender = Mock(return_value=204)
        worker.run_once(self.app.state.pool, settings, sender)
        self.assertEqual(sender.call_args.args[2:4], ("X-New", "new-secret"))

    def test_webhook_filter_changes_invalidate_old_deliveries(self):
        for change in (
            {"url": "https://other.example/events"},
            {"track_ids": []},
            {"editable_events": False},
        ):
            with self.subTest(change=change):
                webhook = self.register()
                path = "/service/webhooks/" + webhook["id"]
                self.send("POST", self.events, test_api.event())
                self.send("PUT", path, {**webhook, **change}, status=204)
                sender = Mock(return_value=204)
                worker.run_once(self.app.state.pool, self.app.state.settings, sender)
                sender.assert_not_called()
                self.send("DELETE", path, status=204)


def test_worker_drains_full_batches_without_poll_delay():
    import threading
    from unittest.mock import MagicMock

    from tadoss.settings import Settings

    stopped = threading.Event()
    waits = []
    calls = []

    def process(*_args):
        calls.append(True)
        if len(calls) == 3:
            assert waits == []
            stopped.set()
        return True

    with (
        patch.object(worker.threading, "Event", return_value=stopped),
        patch.object(stopped, "wait", side_effect=lambda delay: waits.append(delay)),
        patch.object(worker.threading, "Thread"),
        patch.object(worker, "ThreadingHTTPServer"),
        patch.object(worker.signal, "signal"),
        patch.object(store, "create_pool", return_value=MagicMock()),
        patch.object(store, "transaction", return_value=MagicMock()),
        patch.object(store, "check_schema"),
        patch.object(worker, "run_once", side_effect=process),
        patch.object(worker, "maintain") as maintain,
    ):
        worker.main(Settings(dev=True, worker_max_requests=2))
    assert len(calls) == 3
    maintain.assert_called_once()
