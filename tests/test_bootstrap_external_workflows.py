from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_bootstrap_module():
    path = ROOT / "scripts" / "bootstrap_external_workflows.py"
    spec = importlib.util.spec_from_file_location("bootstrap_external_workflows_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_framework_specific_build_and_python_compatibility_contracts():
    module = _load_bootstrap_module()

    assert module.DEFAULT_PYTHONS["finrobot"] == "3.11"
    assert "openai>=1.66.2,<1.67" in module.INSTALL_TARGETS["finrobot"]
    assert module.BUILD_ENVIRONMENTS["qlib"] == {
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYQLIB": "0.0+locked.d5379c5"
    }


def test_qlib_bootstrap_forwards_locked_snapshot_version(tmp_path, monkeypatch):
    module = _load_bootstrap_module()
    monkeypatch.setattr(module, "RUNTIME_ROOT", tmp_path / "venvs")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    calls = []

    def fake_run(command, *, dry_run, environment=None):
        calls.append({"command": command, "dry_run": dry_run, "environment": environment})

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.bootstrap("qlib", python="3.12", dry_run=True)

    assert result["STOCK_AI_QLIB_PYTHON"].endswith("venvs/qlib/bin/python")
    install = calls[-1]
    assert install["environment"] == module.BUILD_ENVIRONMENTS["qlib"]
    assert str(ROOT / "external" / "qlib") in install["command"]


def test_finrobot_bootstrap_keeps_required_openai_compatibility_client(tmp_path, monkeypatch):
    module = _load_bootstrap_module()
    monkeypatch.setattr(module, "RUNTIME_ROOT", tmp_path / "venvs")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    calls = []
    monkeypatch.setattr(
        module,
        "_run",
        lambda command, *, dry_run, environment=None: calls.append(command),
    )

    module.bootstrap("finrobot", python="3.11", dry_run=True)

    install = calls[-1]
    assert "openai>=1.66.2,<1.67" in install
    assert str(ROOT / "external" / "FinRobot") in install
