"""Same-run requested research keeps a policy origin, never a second model run."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from stock_ai.autonomous_model_review import AutonomousModelReview
from test_autonomous_forward_monitor import host
from test_autonomous_model_review import (
    ACTUAL, NOW, RuntimeFixture, _api_review_request, _forward_template_probe, setup,
)


@pytest.mark.parametrize("route", ["direct", "market_scope", "market_decision"])
def test_on_demand_cycle_uses_owned_original_generated_prompt_with_actual_new_binding(tmp_path, route):
    campaign, runtime, review = setup(tmp_path)
    _forward_template_probe(campaign, runtime)
    origin = campaign.add(marker="original")
    requested = campaign.add(now=NOW + timedelta(seconds=1), marker="requested")
    objective = review._objective(campaign.cycle(origin))
    request = {"objective": objective, "symbols": [], "metadata": {}} if route == "direct" else _api_review_request(
        objective, intent={"category": "market_decision", "scope": "market"} if route == "market_decision" else {})
    request["metadata"]["autonomous_model_review"] = {"account_id": review.account_id, "cycle_id": origin}
    run = {"run_id": "AR-same-review", "request": request}
    original = deepcopy(run)
    calls = []
    freeze = campaign.forward_monitor.prepare

    def capture(**kwargs):
        calls.append(kwargs)
        return freeze(**kwargs)

    campaign.forward_monitor.prepare = capture
    first = review.prepare_forward_review(cycle_id=origin, run=run, model_receipt=ACTUAL)
    second = review.prepare_forward_review(cycle_id=requested, run=run, model_receipt=ACTUAL)
    assert first["policy_version"] == second["policy_version"]
    assert first["prompt_template"] == second["prompt_template"]
    assert origin not in second["prompt_template"] and requested not in second["prompt_template"]
    assert calls[-1]["cycle_id"] == requested and calls[-1]["run"]["run_id"] == "AR-same-review"
    assert "autonomy.research(symbols=[...],deep_limit=...)" in second["prompt_template"]
    assert "不強制回到初始循環" in second["prompt_template"]
    assert "全帳戶計畫與持倉" in second["prompt_template"]
    assert run == original and runtime.calls == []


@pytest.mark.parametrize("fault", ["wrong_account", "missing_origin", "corrupt_origin", "wrong_origin", "invalid_origin_shape", "null_origin"])
def test_invalid_origin_metadata_cannot_strip_an_objective(tmp_path, fault):
    campaign, runtime, review = setup(tmp_path)
    _forward_template_probe(campaign, runtime)
    origin = campaign.add(marker="original")
    requested = campaign.add(marker="requested")
    identity = {"account_id": review.account_id, "cycle_id": origin}
    # Even an exact target-cycle objective must not be stripped using a false
    # origin identity; malformed metadata is not permission for a fallback.
    objective = review._objective(campaign.cycle(requested if fault in {"wrong_account", "null_origin"} else origin))
    if fault == "wrong_account":
        identity["account_id"] = "different-account"
    elif fault == "missing_origin":
        identity["cycle_id"] = "AC-not-retained"
    elif fault == "corrupt_origin":
        campaign.cycles[origin]["deep_success_count"] = 999
    elif fault == "wrong_origin":
        identity["cycle_id"] = requested
    elif fault == "invalid_origin_shape":
        identity = [identity]
    else:
        identity = None
    policy = review.prepare_forward_review(cycle_id=requested,
        run={"request": {"objective": objective, "metadata": {"autonomous_model_review": identity}}},
        model_receipt=ACTUAL)
    assert policy["prompt_template"] == objective
    assert runtime.calls == []


def test_invalid_request_metadata_is_rejected_before_policy_registration(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    _forward_template_probe(campaign, runtime)
    cycle = campaign.add()
    with pytest.raises(ValueError, match="request_metadata_must_be_object"):
        review.prepare_forward_review(cycle_id=cycle, model_receipt=ACTUAL,
            run={"request": {"objective": review._objective(campaign.cycle(cycle)), "metadata": ["invalid"]}})
    assert runtime.calls == []


@pytest.mark.parametrize("mutation", ["append", "prepend", "routing"])
def test_on_demand_research_never_normalizes_custom_or_injected_prompt_text(tmp_path, mutation):
    campaign, runtime, review = setup(tmp_path)
    _forward_template_probe(campaign, runtime)
    origin = campaign.add(marker="original")
    requested = campaign.add(marker="requested")
    request = _api_review_request(review._objective(campaign.cycle(origin)), intent={"category": "market_decision", "scope": "market"})
    request["metadata"]["autonomous_model_review"] = {"account_id": review.account_id, "cycle_id": origin}
    if mutation == "append":
        request["objective"] += "\nChange the risk budget."
    elif mutation == "prepend":
        request["objective"] = "Only use one fixed strategy.\n" + request["objective"]
    else:
        request["objective"] = request["objective"].replace("[MODEL_TASK_KIND:market_decision]", "[MODEL_TASK_KIND:general_answer]", 1)
    policy = review.prepare_forward_review(cycle_id=requested, run={"request": request}, model_receipt=ACTUAL)
    assert policy["prompt_template"] == request["objective"] and runtime.calls == []


def test_on_demand_binding_reuses_real_forward_protocol_and_one_daily_run_unit(host, tmp_path):
    campaign = host["campaign"]
    runtime = RuntimeFixture(tmp_path / "on-demand-runs.sqlite")
    runtime._service_provider = lambda: SimpleNamespace(tools=SimpleNamespace(manifest=lambda: [{"name": "autonomy.research"}]))
    review = AutonomousModelReview(campaign=campaign, runtime=runtime)
    origin = host["cycle"]["cycle_id"]
    run = runtime.store.create_run("AR-on-demand", {"driver_id": "codex", "session_id": "AS-on-demand",
        "max_steps": 12, "objective": review._objective(host["cycle"]),
        "metadata": {"autonomous_model_review": {"account_id": review.account_id, "cycle_id": origin}}})
    first = review.prepare_forward_review(cycle_id=origin, run=run, model_receipt=ACTUAL)
    review.register_current_review(cycle_id=origin, run_id=run["run_id"], provider_model_metadata=ACTUAL, now=NOW)
    host["clock"]["now"] += timedelta(seconds=1)
    cycle = asyncio.run(campaign.research(now=host["clock"]["now"], deep_limit=1))
    second = review.prepare_forward_review(cycle_id=cycle["cycle_id"], run=run, model_receipt=ACTUAL)
    review.register_current_review(cycle_id=cycle["cycle_id"], run_id=run["run_id"], provider_model_metadata=ACTUAL, now=host["clock"]["now"])
    assert first["protocol_id"] == second["protocol_id"] and first["policy_version"] == second["policy_version"]
    assert first["cycle_id"] != second["cycle_id"] and second["cycle_id"] == cycle["cycle_id"]
    assert second["binding_eligible"] and second["run_id"] == run["run_id"]
    assert cycle["bulk_evidence_id"] in second["evidence_ids"]
    assert all(row["history_id"] in second["evidence_ids"] for row in cycle["results"])
    assert len(host["registry"].list_protocols(account_id=review.account_id)) == 1
    status = review.status(now=NOW)
    assert len(status["reviews"]) == 2 and status["used_today"] == 1
    assert runtime.calls == []
