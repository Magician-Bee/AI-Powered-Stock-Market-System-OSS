from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts" / "verify_autonomous_closed_loop.py"
    spec = importlib.util.spec_from_file_location("verify_autonomous_closed_loop", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_task_reaches_analysis_orders_fills_management_reconciliation_and_learning(tmp_path):
    receipt = _module().run_verification(tmp_path / "closed-loop.sqlite")
    assert receipt["passed"] is True
    assert [stage["stage"] for stage in receipt["stages"]] == [
        "analysis", "plan", "entry_submission",
        "position_management_and_exit_submission", "exit_fill_and_reconciliation",
    ]
    assert receipt["final_account"]["order_count"] == 2
    assert receipt["final_account"]["fill_count"] == 2
    assert receipt["final_account"]["position_count"] == 0
    assert receipt["outcome"]["net_pnl"] == pytest.approx(receipt["final_account"]["realized_pnl"], abs=0.01)
    assert receipt["outcome"]["commission"] > 0
    assert receipt["outcome"]["tax"] > 0
    assert receipt["outcome"]["positive_ev_qualified"] is False
