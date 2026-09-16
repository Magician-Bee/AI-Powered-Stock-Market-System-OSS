"""Validate tracked public source and a bounded offline regression subset.

Requires the project's dev environment and a separately installed Gitleaks.
Never launches the application, a model, a browser, a broker or notifications.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    'tests/test_risk_control.py',
    'tests/test_portfolio_risk_constraints.py',
    'tests/test_paper_account_safety.py',
    'tests/test_broker_framework.py',
    'tests/test_broker_transport_guard.py',
)
HOME_PATH = re.compile(r'(?<![A-Za-z0-9_./-])(?:/Users/|/home/|[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2})([A-Za-z0-9_.-]+)')
EXAMPLE_USERS = {'your-user','your-name','user','username','example','test','tester','alice','bob','runner','public','default','developer','dev','root'}
BLOCKED_TOP = {'.runtime','.venv','.tools','logs','output','artifacts','models','node_modules'}
BLOCKED_SUFFIX = {'.lnk','.pem','.key','.p12','.pfx','.db','.sqlite','.sqlite3','.pyc','.log'}


def tracked_paths() -> list[Path]:
    result = subprocess.run(['git','ls-files','-z'], cwd=ROOT, capture_output=True, check=True)
    return [Path(os.fsdecode(value)) for value in result.stdout.split(b'\0') if value]



def indexed_sha256(excluded=()):
    import io
    rows=subprocess.run(['git','ls-files','-s','-z'],cwd=ROOT,capture_output=True,check=True).stdout
    entries={}
    for row in rows.split(b'\0'):
        if not row:continue
        meta,name=row.split(b'\t',1)
        name=os.fsdecode(name)
        if name not in excluded:entries[name]=meta.split()[1].decode()
    unique=list(dict.fromkeys(entries.values()))
    raw=subprocess.run(['git','cat-file','--batch'],cwd=ROOT,input=('\n'.join(unique)+'\n').encode(),capture_output=True,check=True).stdout
    stream=io.BytesIO(raw);hashes={}
    for sha in unique:
        header=stream.readline().split();assert header[0].decode()==sha and header[1]==b'blob'
        content=stream.read(int(header[2]));assert stream.read(1)==b'\n'
        hashes[sha]=hashlib.sha256(content).hexdigest()
    return {name:hashes[sha] for name,sha in entries.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gitleaks', default='gitleaks')
    parser.add_argument('--output', type=Path, default=ROOT/'output/public-validation.json')
    args = parser.parse_args()
    scanner = shutil.which(args.gitleaks)
    if scanner is None:
        parser.error('Gitleaks executable is required; install it from the official release.')
    scanner = str(Path(scanner).resolve())
    paths = tracked_paths()
    issues: list[dict] = []
    python_count = 0
    notebook_count = 0
    for rel in paths:
        path = ROOT/rel
        if (rel.parts[0] in BLOCKED_TOP or path.name.startswith('._') or
            path.name in {'.DS_Store','.gitleaksignore'} or path.suffix.lower() in BLOCKED_SUFFIX or
            (path.name.startswith('.env') and path.name != '.env.example')):
            issues.append({'file':rel.as_posix(),'check':'private or generated file'})
            continue
        if path.is_symlink() or not path.is_file():
            issues.append({'file':rel.as_posix(),'check':'unsupported file type'})
            continue
        raw = path.read_bytes()
        if path.suffix == '.py':
            python_count += 1
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', SyntaxWarning)
                    compile(raw, rel.as_posix(), 'exec')
            except SyntaxError as error:
                issues.append({'file':rel.as_posix(),'line':error.lineno,'check':'Python syntax'})
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            continue
        if any(match[1].lower() not in EXAMPLE_USERS for match in HOME_PATH.finditer(text)):
            issues.append({'file':rel.as_posix(),'check':'personal home path'})
        if path.suffix == '.ipynb':
            notebook_count += 1
            try:
                nb = json.loads(text)
                for cell in nb.get('cells', []):
                    if cell.get('outputs') or cell.get('execution_count') is not None:
                        issues.append({'file':rel.as_posix(),'check':'stored notebook output'})
                        break
            except (ValueError, TypeError):
                issues.append({'file':rel.as_posix(),'check':'invalid notebook'})
    cfg = (ROOT/'config/capability_status.yaml').read_text(encoding='utf-8')
    if not all(item in cfg for item in ('current_level: PAPER','live_order_submission_enabled: false','model_direct_broker_submission_enabled: false')):
        issues.append({'file':'config/capability_status.yaml','check':'PAPER-only default'})

    checks = [{'name':'tracked_source_hygiene','files_checked':len(paths),'python_files_compiled':python_count,
               'notebooks_checked':notebook_count,'review_items':issues,'passed':not issues}]
    with tempfile.TemporaryDirectory(prefix='stock-public-validation-') as directory:
        temporary = Path(directory)
        scan = temporary/'source'
        scan.mkdir()
        for rel in paths:
            path = ROOT/rel
            if path.is_file() and not path.is_symlink():
                dest = scan/rel
                dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,dest)
        scan_report = temporary/'gitleaks.json'
        result = subprocess.run([scanner,'dir',str(scan),'--no-banner','--redact=100',
            '--report-format=json','--report-path',str(scan_report)],capture_output=True,timeout=120)
        findings = json.loads(scan_report.read_text()) if scan_report.exists() else []
        scanner_version = subprocess.run([scanner,'version'],capture_output=True,text=True,check=True).stdout.strip()
        checks.append({'name':'gitleaks','version':scanner_version,'exit_code':result.returncode,
                       'findings':len(findings),'passed':result.returncode==0 and not findings})
        if issues or result.returncode or findings:
            print('Public source review failed; inspect redacted filenames, not credentials.')
        else:
            env = {k:v for k,v in os.environ.items() if not any(part in k.upper() for part in ('TOKEN','SECRET','PASSWORD','API_KEY','GIT_CONFIG_'))}
            env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                       PYTHONPATH=os.pathsep.join((str(ROOT/'scripts'),str(ROOT/'src'))),
                       OPEN_STOCK_AI_ENV_FILE=str(temporary/'absent.env'),
                       STOCK_AI_AGENT_BACKGROUND_PAUSED='1',
                       OPEN_STOCK_AI_MODE='paper', LIVE_TRADING_ENABLED='false')
            junit = temporary/'pytest.xml'
            started = time.monotonic()
            command = [sys.executable,'-m','pytest','-p','public_release_no_network',*TESTS,'-q','--junitxml='+str(junit)]
            result = subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,timeout=180)
            cases = ET.parse(junit).getroot().findall('.//testcase') if junit.exists() else []
            failed = sum(case.find('failure') is not None for case in cases)
            errors = sum(case.find('error') is not None for case in cases)
            skipped = sum(case.find('skipped') is not None for case in cases)
            checks.append({'name':'offline_regression_subset','test_files':TESTS,'exit_code':result.returncode,
                'seconds':round(time.monotonic()-started,3),'tests':len(cases),'failed':failed,'errors':errors,'skipped':skipped,
                'passed_count':len(cases)-failed-errors-skipped,'network_connections_blocked':True,
                'passed':result.returncode==0 and bool(cases) and failed==errors==0})
            if result.returncode:
                print('Offline regression failed; raw test output is intentionally not published automatically.')
    versions = {}
    for package in ('pytest','pydantic','pydantic-settings','httpx','PyYAML','pandas','hypothesis'):
        try: versions[package] = version(package)
        except PackageNotFoundError: pass
    excluded = {'docs/evidence/SOURCE-SHA256.json','docs/evidence/public-validation.json'}
    manifest = indexed_sha256(excluded)
    report = {'schema_version':1,'release':'v0.1.0-public.20260916','checked_at_utc':datetime.now(timezone.utc).isoformat(),
        'environment':{'os':platform.system(),'architecture':platform.machine(),'python':platform.python_version()},
        'dependency_versions':versions,'checks':checks,'passed':all(check['passed'] for check in checks),
        'application_started':False,'real_model_calls':0,'real_broker_calls':0,
        'scope':'tracked source checks and selected offline regression; not full product acceptance or trading performance',
        'source_files_sha256':hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'limitations':['Pattern scanning does not prove absence of all identifiers or secrets.','Historical model results are not rerun.','Cross-platform installation and live broker execution are not certified.']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'checks':checks},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
