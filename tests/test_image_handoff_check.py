import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def diagnostic(monkeypatch):
    path = Path(__file__).parents[1] / 'scripts/check-image-handoff.py'
    spec = importlib.util.spec_from_file_location('image_handoff_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('problem', [None, 'mismatch', 'missing', 'unready', 'parked-no-cuda', 'mount_mismatch'])
def test_diagnostic_checks_readiness_mounts_and_keys_without_leaking_fingerprints(diagnostic, monkeypatch, problem):
    calls = []
    def command(arguments, **kwargs):
        calls.append(arguments)
        if 'inspect' in arguments:
            source = '/other/data/video' if problem == 'mount_mismatch' and arguments[-1] == 'imagegen-container' else '/host/data/video'
            return SimpleNamespace(returncode=0, stdout=json.dumps([{'Type':'bind','Source':source,'Destination':'/control','RW':False}]))
        if 'ps' in arguments:
            return SimpleNamespace(returncode=0, stdout=arguments[-1]+'-container\n')
        service = arguments[arguments.index('-T')+1]
        code = arguments[-1]
        if code == diagnostic.KEY_CHECK:
            key = {'path':'/control/model-control.key','exists':True,'readable':True,'nonempty':True,'fingerprint':'private-fingerprint'}
            if service == 'imagegen' and problem == 'mismatch': key['fingerprint']='other-private-fingerprint'
            if service == 'imagegen' and problem == 'missing': key.update(exists=False, readable=False, nonempty=False)
            result = key
        elif code == diagnostic.APP_CHECK:
            result = {'handoff_enabled':True}
        else:
            result = {'ok':service=='chatterbox','parked':service=='imagegen','cuda_visible':True}
            if service=='imagegen' and problem=='unready': result['parked']=False
            if service=='imagegen' and problem=='parked-no-cuda': result['cuda_visible']=False
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))
    monkeypatch.setattr(diagnostic.subprocess, 'run', command)
    result = diagnostic.check(['docker-compose.yml', 'docker-compose.gpu.yml'])
    assert result['ok'] == (problem is None)
    assert 'private-fingerprint' not in json.dumps(result)
    assert result['services']['imagegen']['control_mounts'][0]['RW'] is False
    assert all('/models/load' not in str(c) and '/models/unload' not in str(c) for c in calls)


def test_diagnostic_reports_daemon_failure_without_echoing_stderr(diagnostic, monkeypatch):
    monkeypatch.setattr(diagnostic.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='secret-error-text'))
    result = diagnostic.check(['docker-compose.yml'])
    assert not result['ok']
    assert 'secret-error-text' not in json.dumps(result)
    assert len(result['errors']) == 3
    assert result['keys_match'] is None and result['control_sources_match'] is None
    assert 'disabled' not in str(result['errors'])
