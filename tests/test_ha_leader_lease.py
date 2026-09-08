from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.governance import LeaseNotAcquired, SQLiteLeaderLease, ServiceTopology


def test_sqlite_leader_lease_fences_old_owner_after_takeover(tmp_path) -> None:
    now = [datetime(2026, 8, 26, tzinfo=timezone.utc)]
    store = SQLiteLeaderLease(tmp_path / "ha.sqlite", ttl_seconds=30, clock=lambda: now[0])
    primary = store.claim("broker-gateway", "node-a")

    with pytest.raises(LeaseNotAcquired):
        store.claim("broker-gateway", "node-b")

    now[0] += timedelta(seconds=31)
    secondary = store.claim("broker-gateway", "node-b")
    assert secondary.epoch == primary.epoch + 1
    assert primary.verify() and secondary.verify()
    with pytest.raises(LeaseNotAcquired):
        store.renew("broker-gateway", "node-a", primary.epoch)
    renewed = store.renew("broker-gateway", "node-b", secondary.epoch)
    assert renewed.epoch == secondary.epoch
    assert store.current("broker-gateway")["owner_id"] == "node-b"


def test_ha_topology_names_separate_services_and_fencing_contract() -> None:
    topology = ServiceTopology()
    payload = topology.as_dict()
    assert set(payload["services"]) == {"api", "agent_runtime", "market_data", "broker_gateway", "scheduler"}
    assert payload["leader_lease"] is True
    assert payload["fencing_token"] is True
