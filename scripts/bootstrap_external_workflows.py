from __future__ import annotations

"""Create isolated runtimes for the optional full finance workflows.

The main application deliberately does not install GPU/model frameworks into
its core environment.  This command creates one runtime per selected upstream
project and prints the environment variable that binds it to the Agent.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / ".runtime" / "external-workflows" / "venvs"

INSTALL_TARGETS = {
    "tradingagents": [str(ROOT / "external" / "TradingAgents")],
    "fingpt": [
        "torch>=2.2,<3",
        "transformers>=4.45,<5",
        "peft>=0.13,<1",
        "accelerate>=0.34,<2",
        "structlog>=24,<26",
    ],
    "finrl": [
        "torch>=2.2,<3",
        "stable-baselines3>=2.3,<3",
        "gymnasium>=1,<2",
        "numpy>=1.26",
        "pandas>=2.2",
        "pyyaml>=6",
        "matplotlib>=3.8",
        "tensorboard>=2.16",
    ],
    "qlib": [str(ROOT / "external" / "qlib")],
    # FinRobot declares pyautogen, while current AG2 keeps its OpenAI-compatible
    # client as an optional dependency.  The Codex bridge speaks that protocol,
    # so install the client explicitly without requiring an OpenAI API key.
    # AG2 0.9's optional-client guard requires the 1.66 API surface and
    # misclassifies later 1.100+ releases, so keep this narrow compatibility
    # band until the locked FinRobot snapshot is upgraded.
    "finrobot": [str(ROOT / "external" / "FinRobot"), "openai>=1.66.2,<1.67"],
}

ENV_NAMES = {
    "tradingagents": "STOCK_AI_TRADINGAGENTS_PYTHON",
    "fingpt": "STOCK_AI_FINGPT_PYTHON",
    "finrl": "STOCK_AI_FINRL_PYTHON",
    "qlib": "STOCK_AI_QLIB_PYTHON",
    "finrobot": "STOCK_AI_FINROBOT_PYTHON",
}

DEFAULT_PYTHONS = {
    # Upstream FinRobot declares python_requires >=3.10,<3.12 and pins
    # pandas==2.0.3, which has no CPython 3.12 wheel.
    "finrobot": "3.11",
}

# The repository vendors source snapshots without each upstream project's
# nested ``.git`` directory.  Qlib derives its version with setuptools-scm,
# so an isolated build needs an explicit, deterministic snapshot version.
# Keep the value tied to config/external_sources.lock.yaml's locked commit.
BUILD_ENVIRONMENTS = {
    "qlib": {
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYQLIB": "0.0+locked.d5379c5",
    },
}


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _runtime_is_compatible(framework: str, interpreter: Path) -> bool:
    if not interpreter.is_file():
        return False
    if framework != "finrobot":
        return True
    result = subprocess.run(
        [str(interpreter), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() in {"3.10", "3.11"}


def _run(command: list[str], *, dry_run: bool, environment: dict[str, str] | None = None) -> None:
    print(" ".join(command))
    if not dry_run:
        child_environment = os.environ.copy()
        child_environment.update(environment or {})
        subprocess.run(command, cwd=ROOT, env=child_environment, check=True)


def bootstrap(framework: str, *, python: str, dry_run: bool) -> dict[str, str]:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required; launch the project once or install uv first")
    venv = RUNTIME_ROOT / framework
    interpreter = _venv_python(venv)
    if interpreter.is_file() and not _runtime_is_compatible(framework, interpreter) and not dry_run:
        shutil.rmtree(venv)
    if not interpreter.is_file() or dry_run:
        _run([uv, "venv", str(venv), "--python", python, "--seed"], dry_run=dry_run)
    _run(
        [uv, "pip", "install", "--python", str(interpreter), *INSTALL_TARGETS[framework]],
        dry_run=dry_run,
        environment=BUILD_ENVIRONMENTS.get(framework),
    )
    return {ENV_NAMES[framework]: str(interpreter)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "frameworks",
        nargs="+",
        choices=[*INSTALL_TARGETS, "all"],
        help="One or more isolated framework runtimes to prepare",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Override the base Python used for every selected venv (defaults are framework-compatible)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without installing packages")
    args = parser.parse_args()
    selected = list(INSTALL_TARGETS) if "all" in args.frameworks else list(dict.fromkeys(args.frameworks))
    bindings: dict[str, str] = {}
    for framework in selected:
        base_python = args.python or DEFAULT_PYTHONS.get(framework) or sys.executable
        bindings.update(bootstrap(framework, python=base_python, dry_run=args.dry_run))
    print(json.dumps({"bindings": bindings, "dry_run": args.dry_run}, ensure_ascii=False, indent=2))
    if not args.dry_run:
        binding_path = RUNTIME_ROOT.parent / "runtime-bindings.json"
        existing = {}
        if binding_path.is_file():
            try:
                existing = json.loads(binding_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.update(bindings)
        binding_path.parent.mkdir(parents=True, exist_ok=True)
        binding_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        binding_path.chmod(0o600)
        print(f"Saved runtime bindings: {binding_path}")
        print("Set these variables before launching the application:")
        for name, value in bindings.items():
            print(f"export {name}={json.dumps(value)}")


if __name__ == "__main__":
    main()
