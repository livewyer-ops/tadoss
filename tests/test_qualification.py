from types import SimpleNamespace

import pytest

from scripts.platform_preflight import foreign_releases
from scripts.qualify import DISPOSABLE, Cluster, event, redact, reset_fixture


def test_shared_releases_are_reused_only_when_omitted():
    selected = [{"namespace": "cnpg-system", "name": "cnpg"}]
    existing = {("cnpg-system", "cnpg"): "Upgrade complete"}
    assert foreign_releases(selected, existing) == ["cnpg-system/cnpg"]
    assert foreign_releases([], existing) == []
    assert (
        foreign_releases(selected, {("cnpg-system", "cnpg"): "TADOSS-platform"}) == []
    )


@pytest.mark.parametrize(
    "namespace,instance", [(False, False), (True, False), (False, True)]
)
def test_qualification_refuses_unmarked_targets(namespace, instance, monkeypatch):
    cluster = Cluster(
        SimpleNamespace(kubeconfig="test", namespace="production", instance="data")
    )

    def get(kind, _name):
        marked = namespace if kind == "namespace" else instance
        return {"metadata": {"labels": {DISPOSABLE: "true"} if marked else {}}}

    monkeypatch.setattr(cluster, "get", get)
    with pytest.raises(ValueError, match="disposable"):
        cluster.guard()


def test_reports_strip_inline_credentials():
    assert redact(
        {
            "env": [{"name": "TADOSS_API_TOKEN", "value": "secret"}],
            "api_key_value": "secret",
        }
    ) == {
        "env": [{"name": "TADOSS_API_TOKEN", "value": "[redacted]"}],
        "api_key_value": "[redacted]",
    }


def test_fixture_event_ids_are_repeatable_and_track_scoped():
    one = "f3d40676-11ee-4074-88a5-000000000000"
    two = "eed40676-11ee-4074-88a5-000000000000"
    assert event(one, 1000, 8)["id"] == "f3d40676-11ee-4074-88a5-0000000003e8"
    assert event(one, 1000, 8)["id"] != event(two, 1000, 8)["id"]


def test_matrix_never_resets_a_database_with_unrecognised_tracks():
    statements = []

    def query(statement):
        statements.append(statement)
        return '["someone-elses-track"]'

    cluster = SimpleNamespace(guard=lambda: None, sql=query)
    with pytest.raises(ValueError, match="exclusive ownership"):
        reset_fixture(cluster, None, {"tracks": ["owned"], "restricted": "restricted"})
    assert len(statements) == 1 and statements[0].startswith("SELECT")
