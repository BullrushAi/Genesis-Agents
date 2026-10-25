"""A delivered job must preserve the complete validated result through Postgres."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import job_store
import worker
from canonical_result_contract import validate_canonical_result


@pytest.mark.asyncio
async def test_worker_persists_complete_result_and_json_envelope(pg_env):
    slug = 'genesis-knowledge-evidence'
    value = {
        'agent_id': slug, 'task_id': 'result-integrity', 'scope': 'policy comparison',
        'status': 'ANALYSIS_COMPLETE', 'summary': 'ANALYSIS: Source versions are unavailable.',
        'findings': ['ANALYSIS: ' + ('The source version and effective date remain unknown. ' * 3) for _ in range(8)],
        'proposals': [], 'evidence_refs': [], 'assumptions': [], 'conflicts': [],
        'unknowns': ['UNKNOWN: ' + ('The comparison requires source records. ' * 3) for _ in range(4)],
        'confidence': 0.2, 'risk_flags': [], 'recommended_next_actions': [],
        'requested_human_decisions': [], 'tool_calls_summary': [], 'clarification_needed': False,
    }
    assert validate_canonical_result(slug, value).ok
    response = json.dumps(value)
    assert len(response) > 1800
    trace = {'diagnostic': 'x' * 3000}
    contract = {'required': True, 'ok': True, 'structure_ok': True, 'deterministic_ok': True, 'judge_decision': 'ALLOW'}
    runtime = SimpleNamespace(execute_agent=AsyncMock(return_value={
        'ok': True, 'slug': slug, 'response': response, 'trace': trace, 'output_contract_eval': contract,
    }))
    created = job_store.create_job(agent_slug=slug, prompt='Compare supplied evidence only', params={})
    job = job_store.get_job(created['id'])
    await worker.process_job(job, runtime)
    stored = job_store.get_job(created['id'])
    assert stored['status'] == 'DELIVERED'
    persisted = json.loads(stored['resultSummary'])
    assert persisted['response'] == response
    assert persisted['trace'] == trace
    assert persisted['ok'] is True
    assert persisted['slug'] == slug
    assert persisted['output_contract_eval'] == contract
    recovered = json.loads(persisted['response'])
    assert recovered == value
    assert validate_canonical_result(slug, recovered).ok
