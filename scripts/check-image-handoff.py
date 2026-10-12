#!/usr/bin/env python3
"""Read-only Docker checks. Never print model-control keys or their fingerprints."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

KEY_CHECK = """
import hashlib,json,os
from pathlib import Path
p=Path(os.getenv('MODEL_CONTROL_FILE','/control/model-control.key'))
r={'path':str(p),'exists':p.is_file(),'readable':False}
if r['exists']:
    try:
        content=p.read_bytes()
        r.update(readable=True,nonempty=bool(content.strip()),mode=oct(p.stat().st_mode & 0o777),fingerprint=hashlib.sha256(content.strip()).hexdigest())
    except OSError:
        pass
print(json.dumps(r))
"""
HEALTH_CHECK = """
import json,urllib.request
with urllib.request.urlopen('http://localhost:8000/health',timeout=5) as response:
    health=json.load(response)
print(json.dumps({k:health.get(k) for k in ['ok','parked','cuda_visible','model_loaded','synthesis_ready','device','model','preset','cpu_offload']}))
"""
APP_CHECK = """
import json,os
print(json.dumps({'handoff_enabled':os.getenv('GPU_MODEL_HANDOFF','false').lower()=='true'}))
"""


def run_json(command: list[str]) -> object:
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode:
        # Docker stderr can contain configuration/credentials: don't echo it.
        raise RuntimeError('Docker command failed; verify daemon access and that the service is running')
    return json.loads(result.stdout)


def check(files: list[str]) -> dict:
    compose = ['docker', 'compose']
    for file in files:
        compose += ['-f', file]
    compose += ['--profile', 'local-image']
    report = {'ok': False, 'services': {}, 'errors': []}
    fingerprints = {}
    for service in ['app', 'imagegen', 'chatterbox']:
        item = {}
        report['services'][service] = item
        try:
            key = run_json(compose + ['exec', '-T', service, 'python3', '-c', KEY_CHECK])
            fingerprints[service] = key.pop('fingerprint', None)
            item['control_key'] = key
            if not key.get('readable') or not key.get('nonempty'):
                report['errors'].append(f'{service}: model-control key is missing, unreadable or empty')
            identifiers = subprocess.run(compose + ['ps', '-q', service], capture_output=True,
                                         text=True, timeout=20, check=True).stdout.split()
            if len(identifiers) != 1:
                raise RuntimeError('Expected exactly one running container for this service')
            mounts = run_json(['docker', 'inspect', '--format', '{{json .Mounts}}', identifiers[0]])
            key_path = Path(key['path'])
            item['control_mounts'] = [{k: m.get(k) for k in ['Type', 'Source', 'Destination', 'RW']}
                                     for m in mounts if key_path.is_relative_to(Path(m['Destination']))]
            if item['control_mounts']:
                mount = max(item['control_mounts'], key=lambda m: len(Path(m['Destination']).parts))
                item['control_source'] = str(Path(mount['Source']) / key_path.relative_to(Path(mount['Destination'])))
            if service == 'app':
                item.update(run_json(compose + ['exec', '-T', service, 'python3', '-c', APP_CHECK]))
            else:
                health = run_json(compose + ['exec', '-T', service, 'python3', '-c', HEALTH_CHECK])
                item['health'] = health
                parked = health.get('parked') and health.get('cuda_visible')
                if not health.get('ok') and not parked:
                    report['errors'].append(f'{service}: neither ready nor safely parked with CUDA visible')
        except (RuntimeError, subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            report['errors'].append(f'{service}: cannot inspect container/key/readiness; check Docker access, running services and image versions')
    report['keys_match'] = (bool(fingerprints.get('app')) and len(set(fingerprints.values())) == 1) if len(fingerprints) == 3 else None
    sources = [s.get('control_source') for s in report['services'].values()]
    report['control_sources_match'] = (bool(all(sources)) and len(set(sources)) == 1) if all('control_mounts' in s for s in report['services'].values()) else None
    if report['control_sources_match'] is False:
        report['errors'].append('Control-file mounts do not resolve to one shared host file')
    if report['keys_match'] is False:
        report['errors'].append('Shared control keys do not match across all three services')
    if report['services']['app'].get('handoff_enabled') is False:
        report['errors'].append('App automatic GPU handoffs are disabled; this check targets the modern handoff stack')
    report['ok'] = not report['errors']
    report['note'] = 'Parked with CUDA visible is acceptable before a lease; only a successful model load and smoke test establishes inference readiness. No models were loaded/unloaded by this check.'
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose-file', action='append', dest='files')
    args = parser.parse_args()
    report = check(args.files or ['docker-compose.yml', 'docker-compose.gpu.yml'])
    print(json.dumps(report, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
