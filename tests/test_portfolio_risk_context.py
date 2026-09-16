from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.research.portfolio_construction import PortfolioConstruction
from open_stock_ai.research.portfolio_risk_context import (
    PointInTimePortfolioRiskContextBuilder,
    PortfolioRiskContextStore,
    materialize_session_portfolio_risk_context,
    verify_pit_portfolio_risk_context,
)


_AS_OF = "2026-08-20T09:00:00+00:00"
_MANIFEST = "a" * 64


def _policy(symbols: tuple[str, ...] = ("AAA.TW", "BBB.TW")) -> dict:
    policy = {
        "policy_id": "paper-risk-policy",
        "policy_version": "2026-08-20.v1",
        "account_scope": "paper",
        "account_alias": "paper-default",
        "account_equity": 1_000_000.0,
        "concentration_limits": {
            "issuer": 60.0,
            "industry": 80.0,
            "factor": 1.0,
            "currency": 100.0,
            "broker": 100.0,
            "account": 100.0,
        },
        "factor_limits": {"market": 1.0},
        "max_participation_rate": 0.20,
        "max_days_to_liquidate": 0.20,
        "cvar_confidence": 0.95,
        "max_cvar_pct": 5.0,
        "stress_scenarios": [
            {"scenario_id": "market_gap", "shocks": {symbol: -0.20 for symbol in symbols}}
        ],
        "max_stress_loss_pct": 5.0,
        "approval_status": "approved",
        "approved_by": "paper-owner",
        "approved_at": "2026-08-19T09:00:00+00:00",
    }
    builder = PointInTimePortfolioRiskContextBuilder()
    policy["approval_receipt_sha256"] = builder.issue_account_policy_receipt(policy)
    return policy


def _observation(timestamp: datetime, value: float, revision: str) -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "return": value,
        "available_at": timestamp.isoformat(),
        "source_revision_id": revision,
    }


def _instruments(symbols: tuple[str, ...] = ("AAA.TW", "BBB.TW")) -> dict:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    result = {}
    for offset_symbol, symbol in enumerate(symbols):
        timestamps = [start + timedelta(days=index) for index in range(20)]
        result[symbol] = {
            "symbol": symbol,
            "dataset_manifest_hash": _MANIFEST,
            "historical_pit_eligible": True,
            "production_contract_covered": True,
            "metadata": {
                "issuer_id": symbol.split(".")[0],
                "industry": "technology",
                "currency": "TWD",
                "broker_id": "paper",
                "account_alias": "paper-default",
                "adv_value": 10_000_000.0,
                "adv_available_at": _AS_OF,
                "adv_source_revision_id": f"adv-{symbol}",
            },
            "returns": [
                _observation(
                    timestamp,
                    (0.001 if index % 2 == 0 else -0.0005) * (offset_symbol + 1),
                    f"return-{symbol}-{index}",
                )
                for index, timestamp in enumerate(timestamps)
            ],
            "factor_exposures": {
                "market": {
                    "value": 1.0 + offset_symbol * 0.1,
                    "available_at": _AS_OF,
                    "source_revision_id": f"factor-{symbol}",
                }
            },
            "sizing_inputs": {
                "annualized_volatility_pct": {"value": 20.0, "available_at": _AS_OF, "source_revision_id": f"vol-{symbol}"},
                "expected_win_probability": {"value": 0.60, "available_at": _AS_OF, "source_revision_id": f"prob-{symbol}"},
                "expected_win_pct": {"value": 8.0, "available_at": _AS_OF, "source_revision_id": f"win-{symbol}"},
                "expected_loss_pct": {"value": 4.0, "available_at": _AS_OF, "source_revision_id": f"loss-{symbol}"},
                "max_loss_pct": {"value": 5.0, "available_at": _AS_OF, "source_revision_id": f"stop-{symbol}"},
                "risk_budget_pct": {"value": 1.0, "available_at": _AS_OF, "source_revision_id": f"budget-{symbol}"},
            },
        }
    return result


def test_materializer_emits_one_deterministic_context_for_all_portfolio_risk_engines():
    builder = PointInTimePortfolioRiskContextBuilder()
    first = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=_instruments(),
        account_policy=_policy(),
    )
    second = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=_instruments(),
        account_policy=_policy(),
    )

    assert first == second
    assert first["status"] == "verified"
    assert first["risk_context_status"] == "verified"
    assert first["account_risk_status"] == "verified"
    assert first["blockers"] == []
    assert first["return_timestamps"]
    assert first["covariance_matrix"]["AAA.TW"]["BBB.TW"] == first["covariance_matrix"]["BBB.TW"]["AAA.TW"]
    assert first["position_sizing_inputs"]["AAA.TW"]["risk_budget_pct"] == 1.0
    assert len(first["risk_context_receipt_sha256"]) == 64
    assert len(first["account_risk_receipt_sha256"]) == 64
    assert first["execution_authority"] == "none"
    assert verify_pit_portfolio_risk_context(first) is True


def test_materialized_context_wires_pit_sizing_inputs_into_portfolio_construction():
    builder = PointInTimePortfolioRiskContextBuilder()
    context = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=_instruments(("AAA.TW",)),
        account_policy=_policy(("AAA.TW",)),
    )
    decision = {
        "request": {"symbol": "AAA.TW", "market": "TW", "horizon": "swing"},
        "signal": {"action": "buy", "rule_score": 0.8},
        "research": {"sharpe": 1.2, "raw": {}},
        "risk": {"approved": True, "adjusted_position_size_pct": 25.0},
        "execution": {"executed": False, "mode": "paper"},
    }
    projection = PortfolioConstruction().build([decision], portfolio_risk_context=context)

    assert projection["risk_sizing"]["verified_count"] == 1
    assert projection["positions"][0]["risk_sizing_receipt"]["status"] == "verified"
    assert projection["portfolio_optimization"]["status"] == "verified"
    assert projection["portfolio_risk_constraints"]["status"] == "verified"


def test_session_materializer_requires_one_identical_pit_input_for_every_decision(tmp_path):
    payload = {
        "schema_version": "open_stock_ai.portfolio_risk_materialization_input.v1",
        "as_of": _AS_OF,
        "dataset_manifest_hash": _MANIFEST,
        "instruments": _instruments(),
        "account_policy": _policy(),
    }
    decisions = [
        {
            "request": {"symbol": symbol},
            "market_snapshot": {"raw": {"portfolio_risk_materialization": payload}},
        }
        for symbol in ("AAA.TW", "BBB.TW")
    ]
    store = PortfolioRiskContextStore(tmp_path / "portfolio-session.sqlite")

    context = materialize_session_portfolio_risk_context(decisions, receipt_store=store)

    assert context is not None
    assert context["status"] == "verified"
    assert store.by_receipt(context["risk_context_receipt_sha256"]) == context

    decisions[1]["market_snapshot"]["raw"] = {}
    withheld = materialize_session_portfolio_risk_context(decisions, receipt_store=store)
    assert withheld is not None
    assert withheld["status"] == "withheld"
    assert "portfolio_risk_materialization_missing:BBB.TW" in withheld["blockers"]


def test_materializer_withholds_future_or_unverified_rows_instead_of_using_them():
    instruments = _instruments(("AAA.TW",))
    instruments["AAA.TW"]["returns"][0]["available_at"] = "2026-08-21T09:00:00+00:00"
    builder = PointInTimePortfolioRiskContextBuilder()
    context = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=instruments,
        account_policy=_policy(("AAA.TW",)),
    )

    assert context["status"] == "withheld"
    assert context["risk_context_status"] == "withheld"
    assert context["account_risk_status"] == "withheld"
    assert "portfolio_return_observation_future:AAA.TW:0" in context["blockers"]


def test_risk_engines_withhold_a_materialized_context_after_any_content_tamper():
    builder = PointInTimePortfolioRiskContextBuilder()
    context = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=_instruments(),
        account_policy=_policy(),
    )
    context["account_equity"] = 1.0
    assert verify_pit_portfolio_risk_context(context) is False

    decision = {
        "request": {"symbol": "AAA.TW", "market": "TW", "horizon": "swing"},
        "signal": {"action": "buy", "rule_score": 0.8},
        "research": {"sharpe": 1.2, "raw": {}},
        "risk": {"approved": True, "adjusted_position_size_pct": 25.0},
        "execution": {"executed": False, "mode": "paper"},
    }
    projection = PortfolioConstruction().build([decision], portfolio_risk_context=context)
    assert projection["portfolio_optimization"]["status"] == "withheld"
    assert projection["portfolio_risk_constraints"]["status"] == "withheld"
    assert projection["positions"][0]["risk_sizing_receipt"]["status"] == "withheld"


def test_materializer_rejects_tampered_approval_receipt_and_secret_like_policy_fields():
    builder = PointInTimePortfolioRiskContextBuilder()
    policy = _policy(("AAA.TW",))
    policy["approval_receipt_sha256"] = "0" * 64
    tampered = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=_instruments(("AAA.TW",)),
        account_policy=policy,
    )
    assert tampered["status"] == "withheld"
    assert "portfolio_account_policy_receipt_mismatch" in tampered["blockers"]

    policy = _policy(("AAA.TW",))
    policy["api_key"] = "not-stored"
    blocked = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=_instruments(("AAA.TW",)),
        account_policy=policy,
    )
    assert blocked["status"] == "withheld"
    assert "portfolio_account_policy_contains_secret_like_field" in blocked["blockers"]


def test_portfolio_risk_context_store_replays_verified_and_withheld_receipts_after_restart(tmp_path):
    store = PortfolioRiskContextStore(tmp_path / "portfolio-risk.sqlite")
    builder = PointInTimePortfolioRiskContextBuilder()
    verified = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=_instruments(),
        account_policy=_policy(),
        receipt_store=store,
    )
    withheld = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments={},
        account_policy=_policy(),
        receipt_store=store,
    )

    reopened = PortfolioRiskContextStore(store.path)
    assert reopened.by_receipt(verified["risk_context_receipt_sha256"]) == verified
    assert reopened.by_receipt(withheld["risk_context_receipt_sha256"]) == withheld
    expected = sorted((verified, withheld), key=lambda item: item["risk_context_receipt_sha256"])
    assert reopened.contexts() == expected


def test_portfolio_risk_context_store_is_immutable_and_rejects_database_tampering(tmp_path):
    store = PortfolioRiskContextStore(tmp_path / "portfolio-risk.sqlite")
    context = PointInTimePortfolioRiskContextBuilder().materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=_instruments(),
        account_policy=_policy(),
        receipt_store=store,
    )
    receipt = context["risk_context_receipt_sha256"]
    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.DatabaseError, match="immutable"):
        connection.execute(
            "update portfolio_risk_contexts set status='withheld' where risk_context_receipt_sha256=?",
            (receipt,),
        )
    with sqlite3.connect(store.path) as connection:
        connection.execute("drop trigger portfolio_risk_contexts_immutable_update")
        connection.execute(
            "update portfolio_risk_contexts set payload_json=? where risk_context_receipt_sha256=?",
            ('{"tampered":true}', receipt),
        )
    with pytest.raises(ValueError, match="status_invalid|receipt_invalid|payload_invalid"):
        store.by_receipt(receipt)


def test_portfolio_construction_can_resolve_the_same_context_from_durable_receipt(tmp_path):
    store = PortfolioRiskContextStore(tmp_path / "portfolio-risk.sqlite")
    builder = PointInTimePortfolioRiskContextBuilder()
    context = builder.materialize(
        as_of=_AS_OF,
        dataset_manifest_hash=_MANIFEST,
        instruments=_instruments(("AAA.TW",)),
        account_policy=_policy(("AAA.TW",)),
        receipt_store=store,
    )
    decision = {
        "request": {"symbol": "AAA.TW", "market": "TW", "horizon": "swing"},
        "signal": {"action": "buy", "rule_score": 0.8},
        "research": {"sharpe": 1.2, "raw": {}},
        "risk": {"approved": True, "adjusted_position_size_pct": 25.0},
        "execution": {"executed": False, "mode": "paper"},
    }

    projection = PortfolioConstruction().build(
        [decision],
        portfolio_risk_context_store=store,
        portfolio_risk_context_receipt=context["risk_context_receipt_sha256"],
    )

    assert projection["portfolio_risk_context_source"] == "durable_store"
    assert projection["portfolio_risk_context_receipt"] == context["risk_context_receipt_sha256"]
    assert projection["portfolio_optimization"]["status"] == "verified"
    assert projection["portfolio_risk_constraints"]["status"] == "verified"


def test_portfolio_construction_withholds_when_durable_context_receipt_is_missing(tmp_path):
    store = PortfolioRiskContextStore(tmp_path / "portfolio-risk.sqlite")
    decision = {
        "request": {"symbol": "AAA.TW", "market": "TW", "horizon": "swing"},
        "signal": {"action": "buy", "rule_score": 0.8},
        "research": {"sharpe": 1.2, "raw": {}},
        "risk": {"approved": True, "adjusted_position_size_pct": 25.0},
        "execution": {"executed": False, "mode": "paper"},
    }

    projection = PortfolioConstruction().build(
        [decision],
        portfolio_risk_context_store=store,
        portfolio_risk_context_receipt="f" * 64,
    )

    assert projection["portfolio_risk_context_source"] == "withheld_missing_durable_receipt"
    assert projection["portfolio_optimization"]["status"] == "withheld"
    assert projection["portfolio_risk_constraints"]["status"] == "withheld"
    assert projection["positions"][0]["risk_sizing_receipt"]["status"] == "withheld"
