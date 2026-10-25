"""Regression proof of role transfer and canonical-only reviewer destinations."""
import json
from pathlib import Path

from bundle_loader import load_bundle
from canonical_profiles import CANONICAL_SLUGS, CANONICAL_SOURCE_FILE, check_canonical_inventory

FOLDS = {
    'genesis-canon-steward': 'genesis-knowledge-evidence',
    'genesis-reliability': 'genesis-platform-engineer',
    'genesis-fin-cogs': 'genesis-fin-controller',
    'genesis-fin-commissions': 'genesis-fin-payroll-tax',
    'genesis-fin-intercompany': 'genesis-fin-close',
}
ROOT = Path(__file__).resolve().parents[1]

def test_exact_19_profiles_pass_source_integrity():
    result = check_canonical_inventory()
    assert result['ok'] and result['count'] == 19
    assert result['profile_errors'] == {}

def test_folded_roles_preserve_distinctive_reasoning_and_safety():
    for old, target in FOLDS.items():
        donor, receiver = load_bundle(old), load_bundle(target)
        assert old not in CANONICAL_SLUGS and target in CANONICAL_SLUGS
        assert receiver['absorbed_from'] == [old]
        for invariant in donor['instruction_invariants']:
            assert invariant['source_quote'] in receiver['system_prompt']
        for boundary in donor['role_restrictions']:
            assert boundary in receiver['role_restrictions']
            assert boundary in receiver['system_prompt']
        # Folding never grants provider writes or finance authority.
        assert receiver['profile_chain']['permissions']['provider_write_allowed'] is False
        assert receiver['profile_chain']['permissions']['finance_write_allowed'] is False
        suite = json.loads((ROOT / 'eval/canonical_profiles' / f'{target}.json').read_text())
        assert old in ' '.join(c for d in suite['dimensions'] for c in d['pass_conditions'])

def test_all_review_destinations_are_canonical():
    for slug in CANONICAL_SLUGS:
        assert load_bundle(slug)['source_file'] == CANONICAL_SOURCE_FILE
        for trigger in load_bundle(slug)['reviewer_policy']['triggers']:
            assert set(trigger['reviewer_slugs']) <= CANONICAL_SLUGS
