from pathlib import Path

import pytest

from open_stock_ai.governance import (
    authoritative_store_for,
    authoritative_store_matrix,
    is_authoritative_store,
)


def test_authoritative_store_matrix_is_validated_and_hash_addressed() -> None:
    matrix = authoritative_store_matrix()

    assert matrix["schema_version"] == "open_stock_ai.authoritative_store_matrix.v1"
    assert matrix["validated"] is True
    assert len(matrix["matrix_sha256"]) == 64
    assert len({item["id"] for item in matrix["domains"]}) == len(matrix["domains"])
    assert {item["id"] for item in matrix["domains"]} >= {
        "session", "agent_run", "objective", "forest", "checkpoint", "artifact",
        "automation", "research_experiment", "model", "strategy", "paper_order",
        "live_order", "broker_event", "memory", "observability",
    }


def test_projection_is_explicitly_read_only_and_cannot_be_authority() -> None:
    matrix = authoritative_store_matrix()
    owners = {item["authoritative_store"]["module"] for item in matrix["domains"]}
    for item in matrix["domains"]:
        for projection in item["projections"]:
            assert projection["read_only"] is True
            assert projection["module"] not in owners

    assert is_authoritative_store("paper_order", "src/open_stock_ai/execution/paper_oms.py", "PaperOMS")
    assert not is_authoritative_store("paper_order", "src/open_stock_ai/execution/order_store.py")
    assert is_authoritative_store(
        "research_experiment",
        "src/open_stock_ai/research/experiment_store.py",
        "ExperimentStore",
    )
    assert is_authoritative_store(
        "artifact",
        "src/open_stock_ai/agent_runtime/artifact_store.py",
        "ArtifactStore",
    )
    assert is_authoritative_store(
        "forest",
        "src/open_stock_ai/agent_runtime/forest/projector.py",
        "DurableForestStore",
    )


def test_transitional_domains_keep_explicit_migration_blockers() -> None:
    domains = authoritative_store_matrix()["domains"]
    transitional = [item for item in domains if item["status"] == "transitional"]

    assert next(item for item in domains if item["id"] == "live_order")["status"] == "canonical"
    assert "live_order" not in {item["id"] for item in transitional}
    assert "forest" not in {item["id"] for item in transitional}
    assert "artifact" not in {item["id"] for item in transitional}
    assert "research_experiment" not in {item["id"] for item in transitional}
    assert all(item.get("migration_required") for item in transitional)


def test_unknown_domain_fails_closed() -> None:
    with pytest.raises(KeyError, match="unknown authoritative store domain"):
        authoritative_store_for("not-a-domain")


def test_every_matrix_module_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    for domain in authoritative_store_matrix()["domains"]:
        assert (root / domain["authoritative_store"]["module"]).is_file()
