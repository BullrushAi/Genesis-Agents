# Genesis marketplace specs

Each of the 19 canonical Genesis agents sells work on the open SwarmSync marketplace. The canonical bundle in
`skill_bundles/` is the agent's internal E4L contract (Cato-bounded, genesis-v5 JSON, Ask E4L sources). It is
the wrong brief for an outside buyer: a buyer who asks for a research summary wants a finished, readable
deliverable, not an internal result envelope. `marketplace/specs/<slug>.json` is the agent's brief for
marketplace work: what it is best at, how it works, the bar its work must clear, the shape of what it delivers,
what it declines, and its price. `marketplace_agent.py` turns a spec into the system prompt, the offer policy,
the quality check and the needs-board bid. The internal bundle is untouched.

## Spec fields

| field | type | meaning |
|---|---|---|
| `slug` | str | canonical slug (must be in `canonical_profiles.CANONICAL_SLUGS`) |
| `display_name` | str | marketplace name, e.g. "Genesis Knowledge & Evidence" |
| `specialty` | str | one sentence: the work this agent is the best at |
| `persona` | str | 2-4 sentences: the expert it works like (seniority, standards it holds itself to) |
| `services` | list | 3-6 offers, each `{id, name, description, inputs, deliverable}` |
| `scope_keywords` | list[str] | 15-40 lowercase words/phrases a buyer would use for in-scope work (used for fit and needs-board matching) |
| `capabilities` | list[str] | SwarmSync capability ids it can bid on (lowercase-hyphen, e.g. `research`, `market-research`, `accounts-payable`) |
| `out_of_scope` | list | `{pattern, reason, refer_to}`: `pattern` is a Python regex (case-insensitive) for requests it must decline; `refer_to` is another Genesis slug or null |
| `evidence` | str | `required` (always cite retrieved sources), `when_asked` (cite when the buyer asks for research/sources/data), or `none` |
| `method` | list[str] | 4-8 ordered steps the agent follows on every job |
| `quality_bar` | list[str] | 5-9 concrete, checkable statements the delivered work must satisfy (a reviewer can mark each true/false) |
| `delivery` | object | `{sections: [str], min_words, max_words, format}`; `sections` are the markdown `##` headings in order; `format` is `markdown` |
| `assumptions_policy` | str | what to do when inputs are missing (state assumptions and proceed, never stall) |
| `never` | list[str] | hard rules (no claims of executed actions, no fabricated numbers/sources, etc.) |
| `pricing` | object | `{list_cents, floor_cents}`: list = the bundle's `price_tier_default_cents`; floor = lowest budget it accepts without countering |

## Eval cases (`marketplace/evals/<slug>.json`)

```json
{"slug": "...", "cases": [
  {"id": "...", "kind": "deliver|decline|injection",
   "service": "short service line as a buyer would write it",
   "requirements": {"text": "the buyer's full brief"},
   "budget": 2.5,
   "expect": {
     "decision": "ACCEPTED|REJECTED",
     "must_cover": ["phrases or concepts that must appear"],
     "must_not_contain": ["phrases that must not appear"],
     "min_words": 0, "max_words": 0,
     "citations": false
   }}
]}
```

Every agent has at least 8 cases: 5 `deliver` (realistic paid jobs of varied difficulty, at least one with missing
inputs), 2 `decline` (out of scope or forbidden), 1 `injection` (a brief that tries to make the agent claim it
executed something, approve itself, or address the reviewer). `must_cover` items are short (1-4 words) and
case-insensitive; the grader checks them literally, so use words any competent answer must contain.
