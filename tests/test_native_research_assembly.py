"""Reproduce managed-source assembly with real config and declared service costs.

Only market bars/scanner/account are offline fixtures. The program/config tree,
referenced artifact and nonzero service cost dictionary come from the checkout.
No running service, model, network connection or production database is used.
"""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_managed_source_missing_cost_artifact_fails_then_exact_artifact_restores_research(tmp_path):
    project = Path(__file__).resolve().parents[1]
    assembled = tmp_path / "service-source"
    for folder in ("src", "config"):
        shutil.copytree(project / folder, assembled / folder,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    source = ast.parse((assembled / "src/stock_ai/autonomous_trading_service.py").read_text())
    factory = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "get_autonomous_campaign")
    calls = [node for node in ast.walk(factory) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "AutonomousCampaign"]
    costs = ast.literal_eval(next(keyword.value for keyword in calls[0].keywords if keyword.arg == "costs"))
    assert costs == {"commission_bps": 14.25, "minimum_commission": 20, "slippage_bps": 5,
                     "market_impact_bps": 20, "sell_tax_bps": 30}
    (assembled / "actual_service_costs.json").write_text(json.dumps(costs))
    script = r'''
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sys

def forbidden(*args, **kwargs):
    raise AssertionError("native_assembly_regression_must_not_open_network")
socket.socket.connect = forbidden
socket.create_connection = forbidden

import yaml
from open_stock_ai.research import cost_model
from test_autonomous_campaign import NOW, setup

assembled, project = Path.cwd(), Path(sys.argv[1])
assert Path(cost_model.__file__).resolve().is_relative_to(assembled / "src")
config = yaml.safe_load((assembled / "config/taiwan_market_impact_rules.yaml").read_text())
artifact = assembled / config["calibration_artifact"]
assert not artifact.exists()
costs = json.loads((assembled / "actual_service_costs.json").read_text())
service, _ = setup(assembled / "isolated-account", symbols=("2330.TW", "2308.TW"))
service.costs = costs

async def scenario():
    before = await service.research(now=NOW, deep_limit=2, symbols=["2330.TW", "2308.TW"])
    assert before["deep_success_count"] == 2 and len(before["results"]) == 2
    assert before["errors"] == []
    assert before["candidate_evaluation_success_count"] == 0
    assert before["candidate_evaluation_error_count"] == 4
    for researched in before["results"]:
        assert researched["candidate_evaluation_status"] == "failed"
        assert researched["candidates"] == [] and len(researched["evaluation_errors"]) == 2
        assert all("market impact rules require a calibration artifact and sha256" in error["error"]
                   for error in researched["evaluation_errors"])
        history = service._evidence(researched["history_id"], "price_history")
        assert history["data_evidence"]["symbol"] == researched["symbol"]
        assert len(history["rows"]) == 120
    artifact.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project / config["calibration_artifact"], artifact)
    actual_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert actual_sha == config["calibration_artifact_sha256"]
    after = await service.research(now=NOW, deep_limit=2, symbols=["2330.TW", "2308.TW"])
    assert after["deep_success_count"] == 2 and after["errors"] == []
    assert after["cost_assumptions"] == costs
    qualifications = []
    for result in after["results"]:
        assert len(result["candidates"]) == 2, json.dumps(result, ensure_ascii=False)
        for candidate in result["candidates"]:
            receipt = service._evidence(candidate["qualification_id"], "qualification")
            evaluated_costs = receipt["evaluation_manifest"]["cost_assumptions"]
            assert evaluated_costs["broker_commission_bps"] == 14.25
            assert evaluated_costs["broker_minimum_commission_twd"] == 20
            assert evaluated_costs["adverse_execution_floor_bps"] == 25
            assert set(receipt["partitions"]) == {"train", "validation", "holdout"}
            assert receipt["research_paper_candidate_eligible"] is True
            assert receipt["positive_ev_qualified"] is False
            assert "execution_costs_verified_missing" in receipt["reasons"]
            qualifications.append({"symbol":result["symbol"], "candidate_id":candidate["strategy_id"],
                                   "positive_ev_qualified":receipt["positive_ev_qualified"], "costs":evaluated_costs})
    assert service.broker.submissions == []
    proof = {"source_loaded_from":str(Path(cost_model.__file__).resolve()), "costs":costs,
             "config_sha256":hashlib.sha256((assembled / "config/taiwan_market_impact_rules.yaml").read_bytes()).hexdigest(),
             "artifact_sha256":actual_sha, "before":{"deep_success_count":before["deep_success_count"],
             "candidate_evaluation_error_count":before["candidate_evaluation_error_count"],"errors":before["errors"]},
             "after":{"deep_success_count":after["deep_success_count"],"errors":after["errors"],"qualifications":qualifications},
             "network_calls":0,"model_calls":0,"orders":0,"fixture_data_not_ev_evidence":True}
    (assembled / "native_assembly_result.json").write_text(json.dumps(proof,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(proof,ensure_ascii=False))
asyncio.run(scenario())
'''
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(assembled / "src"), str(project / "tests")]),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", script, str(project)], cwd=assembled, env=environment,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads(result.stdout.strip().splitlines()[-1])
    assert proof["before"]["deep_success_count"] == 2
    assert proof["before"]["candidate_evaluation_error_count"] == 4
    assert proof["after"]["deep_success_count"] == 2
    assert len(proof["after"]["qualifications"]) == 4
