"""Retired tracing configuration cannot load an exporter or change API behavior."""
import os
from pathlib import Path
import subprocess
import sys


def test_runtime_operates_without_exporter_packages_or_collector_configuration(tmp_path):
    script = r"""
import importlib.abc, json, sys
class BlockExporters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'phoenix', 'openinference', 'opentelemetry'}:
            raise AssertionError('retired_exporter_imported')
sys.meta_path.insert(0, BlockExporters())
from fastapi.testclient import TestClient
import main, agent_runtime, worker
client = TestClient(main.app)
response = client.get('/agents', headers={'traceparent': 'malformed'})
assert response.status_code == 200
assert 'traceparent' not in response.headers
body = response.json()
assert len(body['agents'] if isinstance(body, dict) else body) == 19
rejected = client.post('/agents/genesis-fin-ap/run', json={'prompt': 'bounded'})
assert rejected.status_code == 401
assert not any(name.split('.')[0] in {'phoenix', 'openinference', 'opentelemetry'} for name in sys.modules)
print('runtime_without_external_tracing_passed')
"""
    env = {**os.environ, 'GATEWAY_API_KEY': 'local-auth-check', 'PHOENIX_API_KEY': 'retired-test-marker',
           'PHOENIX_COLLECTOR_ENDPOINT': 'https://collector.invalid',
           'PHOENIX_TRACING': 'true', 'GENESIS_AUDIT_DB_PATH': str(tmp_path / 'audit.db')}
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert 'runtime_without_external_tracing_passed' in result.stdout
