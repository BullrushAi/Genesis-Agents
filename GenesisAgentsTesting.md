# GENESIS 24-AGENT — MAXIMUM ARIZE PHOENIX TESTING + OPTIMIZATION DIRECTIVE

You are responsible for taking the **entire 24-agent Genesis system** through the most exhaustive testing, observability, evaluation, failure discovery, optimization, and regression-hardening process reasonably possible using **Arize Phoenix**.

This is an **EXECUTION TASK**, not an audit/reporting exercise.

Do not merely tell me what could be tested.
Do not give me a list of recommendations and stop.
Do not stop because the existing tests pass.
Do not declare something “working” because it executed once.

**FIND THE PROBLEMS. FIX THE PROBLEMS. PROVE THE FIXES. RE-RUN THE TESTS. KEEP GOING.**

Your goal is to make these 24 agents as **correct, reliable, deterministic, efficient, observable, resilient, and production-ready as you can possibly make them.**

---

## 1. FIRST: DISCOVER THE ACTUAL SYSTEM

Inspect the repository and determine from source-of-truth code/configuration:

* all 24 Genesis agents
* purpose/responsibility of each
* allowed tools
* prohibited tools
* prompts/system instructions
* schemas
* inputs/outputs
* model configuration
* routing rules
* guardrails
* dependencies
* external APIs/MCPs
* handoff paths
* shared libraries
* orchestration
* retries
* fallbacks
* state/memory
* persistence
* error handling
* validation
* current tests
* existing Phoenix/OpenTelemetry instrumentation

Do not rely on stale documentation when executable code provides stronger evidence.

Build the test matrix from what actually exists.

Every agent must be tested individually **AND** as part of the larger Genesis system.

---

# 2. INSTRUMENT EVERYTHING IN ARIZE PHOENIX

Phoenix must expose the full execution path.

Use current Phoenix/OpenTelemetry/OpenInference capabilities and instrument at minimum:

**TRACE**

* complete user/request → final answer lifecycle

**AGENT SPANS**

* every agent invocation

**ROUTER SPANS**

* routing/classification decisions

**LLM SPANS**

* every model invocation

**TOOL SPANS**

* every MCP/API/tool call

**CHAIN SPANS**

* intermediate processing

**RETRIEVAL SPANS**

* retrieval/search operations

**GUARDRAIL SPANS**

* policy/validation decisions where applicable

**EVALUATOR SPANS**

* evaluation execution

Capture wherever appropriate:

* agent name
* trace ID
* session ID
* task type
* selected route
* intended agent
* actual agent
* model
* prompt/version
* input
* output
* structured output
* tool selected
* tool arguments
* tool result
* retry count
* fallback usage
* token usage
* latency
* errors
* exception type
* status
* step count
* cost if available
* validation result

Do not expose secrets, credentials, bearer tokens, private keys, or inappropriate sensitive information in traces.

Phoenix should make it possible to answer:

> “Exactly what happened, in what order, why, how long did it take, what did it cost, and where did it go wrong?”

---

# 3. CREATE A REAL PHOENIX EVALUATION DATASET

Do not test these agents with 5 happy-path prompts.

Create serious evaluation datasets covering all 24 agents.

For every major capability include:

### Happy path

Normal expected requests.

### Boundary cases

Inputs at limits of expected behavior.

### Ambiguous cases

Requests where multiple agents/tools appear plausible.

### Incomplete information

Agent must identify what it can/cannot determine.

### Contradictory information

Agent must resolve or surface the conflict correctly.

### Malformed input

Bad JSON, missing fields, unexpected types, malformed tool results, etc.

### Adversarial input

Attempts to force incorrect routing, unsupported behavior, fabricated information, inappropriate tool execution, or instruction conflicts.

### Tool failures

* timeout
* 4xx
* 5xx
* malformed response
* empty response
* connection failure
* authentication failure
* rate limit
* partial result

### Dependency failures

Unavailable MCP/API/database/service.

### Long-context tests

Large but realistic context.

### Distractor tests

Large amounts of irrelevant data around the useful information.

### Stale-data tests

Newest truth conflicts with older information.

### Repeatability tests

Same input repeatedly.

### Multi-turn tests

Correct behavior across a session.

### Cross-agent tests

Tasks requiring multiple Genesis agents.

### Handoff tests

One agent produces information another must consume.

### Recovery tests

A component fails and the system must recover without corrupting the result.

---

# 4. USE MULTIPLE EVALUATION METHODS

Do not trust a single LLM judge.

Use combinations of:

## Deterministic/code evaluators

Whenever correctness can be objectively determined.

Examples:

* schema validity
* expected agent
* expected tool
* prohibited tool not called
* arguments correct
* numerical equality
* output contains required fields
* no unexpected fields
* correct ordering
* status code
* retry limit
* latency threshold
* maximum step count
* duplicate action detection
* idempotency
* exact routing result

## Phoenix / LLM evaluators

Where applicable evaluate:

* correctness
* faithfulness
* hallucination
* relevance
* document relevance
* tool selection
* tool invocation
* conciseness
* refusal correctness
* instruction adherence

Add custom evaluators for Genesis-specific business requirements.

LLM judging should complement deterministic tests, **not replace them**.

---

# 5. TEST ROUTING BRUTALLY

If there is routing/orchestration among these agents, treat routing as a first-class production system.

Create:

* obvious routing cases
* subtle routing cases
* overlapping-domain cases
* ambiguous cases
* misleading wording
* multi-intent requests
* nested intents
* requests requiring sequential agents
* requests requiring parallel agents
* requests that should NOT invoke an agent
* requests that should NOT invoke a tool

Measure:

**routing accuracy = correct destinations / total routing tests**

Also detect:

* unnecessary dispatches
* wrong specialist
* redundant agents
* circular handoffs
* agent ping-pong
* repeated routing
* premature termination
* unnecessary escalation
* missed specialist
* wrong execution order

If the architecture permits it, test **every meaningful agent-to-agent transition**, not just common paths.

---

# 6. TEST TOOL USE AS ITS OWN SYSTEM

For every agent/tool combination determine:

1. Was a tool actually necessary?
2. Was the correct tool selected?
3. Were the correct arguments generated?
4. Was the tool called only the necessary number of times?
5. Was the result interpreted correctly?
6. Was failure handled correctly?
7. Did the agent hallucinate success when the tool failed?
8. Did it attempt an unauthorized/prohibited operation?
9. Did it unnecessarily call multiple equivalent tools?
10. Did it retry intelligently?

Use Phoenix's tool-selection and tool-invocation evaluation capabilities where useful.

Test deliberate traps where:

* two tools look similar
* wrong arguments appear plausible
* a tool returns misleading data
* a tool returns partial data
* tool output contradicts model expectation
* the expected tool is unavailable

---

# 7. DETECT AGENT LOOPS AND PATHOLOGICAL TRAJECTORIES

Analyze traces for:

* repeated LLM calls
* repeated tool calls
* repeated retrieval
* agent loops
* router loops
* unnecessary self-correction
* duplicate handoffs
* endless retries
* oscillating decisions
* repeated parsing
* repeated context loading

Track:

* total steps
* agent transitions
* LLM calls
* tool calls
* retries

Establish reasonable step budgets per task type.

A task that eventually gets the right answer after 42 unnecessary steps is **not healthy**.

---

# 8. TEST DETERMINISM AND VARIANCE

Run important scenarios repeatedly.

Do not assume one successful run proves reliability.

For critical scenarios run enough repetitions to identify:

* intermittent routing failures
* output variance
* tool selection variance
* nondeterministic schema errors
* occasional hallucinations
* occasional retry loops
* sporadic latency spikes

Compare distributions, not merely single executions.

If one run in twenty catastrophically fails, investigate it.

---

# 9. TEST MULTI-AGENT BEHAVIOR

Create realistic end-to-end scenarios requiring several Genesis agents.

Validate:

* correct decomposition
* correct ordering
* correct parallelization
* correct handoffs
* no information loss
* no contradictory conclusions
* no duplicated work
* no unnecessary agents
* final synthesis uses the authoritative evidence
* downstream agents correctly consume upstream structured output

Look specifically for the classic multi-agent failure:

> Every individual agent appears correct, but the combined system is wrong.

---

# 10. TEST STATE AND CONTEXT ISOLATION

Where relevant, test:

* fresh sessions
* long sessions
* repeated requests
* conflicting prior instructions
* one user's/session's context influencing another
* stale state
* partial state
* interrupted execution
* resumed execution

Make sure historical context helps when appropriate but does not contaminate unrelated tasks.

---

# 11. TEST FAILURE RECOVERY

Deliberately break things.

Simulate realistic failures and see whether Genesis survives.

Examples:

* MCP unavailable
* database unavailable
* malformed tool payload
* model timeout
* invalid structured output
* partial downstream outage
* retrieval returns nothing
* retrieval returns bad evidence
* agent throws exception
* orchestration process interrupted
* rate limit
* transient network errors

Evaluate:

* retry correctness
* retry count
* fallback correctness
* error propagation
* partial-result behavior
* duplicate side effects
* data corruption
* user-facing error quality

**Never treat “it threw an error” as sufficient error handling.**

---

# 12. PERFORMANCE + COST OPTIMIZATION

Use Phoenix traces to locate unnecessary expense and latency.

Measure by agent and task:

* p50 latency
* p95 latency
* p99 latency where sample size permits
* model calls
* input tokens
* output tokens
* total tokens
* tool calls
* retrieval calls
* agent transitions
* retries
* steps
* cost

Find:

* bloated prompts
* unnecessarily large contexts
* repeated context
* duplicated retrieval
* oversized outputs
* unnecessary reasoning loops
* model overkill
* serial operations that could safely run concurrently
* repeated tool calls
* unnecessarily broad searches

Optimize them without reducing correctness.

**Correctness and reliability come before token savings.**

---

# 13. PROMPT OPTIMIZATION

Use experiment results to find prompt weaknesses.

Do not randomly rewrite prompts.

For each proposed prompt change:

**BASELINE → CHANGE → PHOENIX EXPERIMENT → COMPARE → KEEP/REJECT**

Evaluate effects on:

* accuracy
* routing
* tool selection
* tool arguments
* hallucination
* instruction following
* schema compliance
* verbosity
* latency
* tokens
* cost

Do not improve one benchmark by quietly destroying another.

---

# 14. MODEL / CONFIGURATION OPTIMIZATION

Where architecture allows configuration changes, experimentally test whether different:

* models
* temperatures
* reasoning levels
* retry policies
* timeout settings
* context strategies
* tool descriptions
* structured-output schemas

produce meaningfully better results.

Never swap models based on intuition alone.

Use Phoenix experiments and measured results.

---

# 15. TURN EVERY DISCOVERED BUG INTO A PERMANENT REGRESSION TEST

This is mandatory.

Whenever you discover a failure:

1. capture the failing trace
2. determine root cause
3. create the smallest reliable reproduction
4. add it to the Phoenix regression dataset/test suite
5. fix the root cause
6. rerun the test
7. rerun related tests
8. rerun broad regression tests

The system must become progressively harder to break.

**Every production-worthy bug discovered should leave behind a test.**

---

# 16. PHOENIX CI QUALITY GATES

Connect important Phoenix-backed evaluations to the normal test/CI flow where practical.

Create hard acceptance criteria appropriate to each test class.

Critical deterministic requirements should generally require:

**100% pass rate.**

Examples:

* schema compliance
* prohibited actions
* authorization constraints
* deterministic accounting/business rules
* required routing invariants
* destructive-operation constraints

Statistical/LLM-evaluated metrics should have explicitly justified thresholds.

A regression below a gate should fail CI rather than merely create a dashboard warning.

---

# 17. MANUALLY INSPECT BAD AND SUSPICIOUS TRACES

Metrics alone are insufficient.

Inspect traces for:

* failures
* evaluator disagreement
* very high latency
* very high token usage
* many tool calls
* many steps
* repeated agent transitions
* retries
* unusually low evaluator scores
* outcomes that technically passed but look suspicious

Use trace analysis to discover problems our existing tests failed to anticipate.

Then convert those discoveries into tests.

---

# 18. ROOT-CAUSE FAILURE CLUSTERING

Do not treat 50 failing cases as 50 unrelated bugs.

Cluster failures into causes such as:

* routing
* prompt ambiguity
* retrieval
* context pollution
* tool selection
* malformed arguments
* schema
* parsing
* stale information
* orchestration
* timeout
* retry
* hallucination
* incorrect reasoning
* poor error handling
* model limitation

Prioritize root causes that eliminate entire classes of failures.

---

# 19. PHOENIX PROJECT ORGANIZATION

Make Phoenix genuinely usable after this work.

Organize projects/datasets/experiments so we can quickly compare:

* individual agents
* system-wide performance
* test categories
* baseline vs optimized
* versions
* regressions

Use sensible metadata/tagging so traces can be filtered by:

* Genesis agent
* scenario
* test class
* prompt version
* model
* tool
* run
* environment
* pass/fail

Do not create an unsearchable telemetry landfill.

---

# 20. REQUIRED OPTIMIZATION LOOP

You are not finished after the first test run.

Run this loop:

**DISCOVER**
↓
**INSTRUMENT**
↓
**BASELINE**
↓
**STRESS TEST**
↓
**EVALUATE**
↓
**TRACE FAILURES**
↓
**ROOT CAUSE**
↓
**FIX**
↓
**ADD REGRESSION TEST**
↓
**RUN PHOENIX EXPERIMENT**
↓
**COMPARE AGAINST BASELINE**
↓
**FULL REGRESSION**
↓
**FIND NEXT BOTTLENECK**
↓
**REPEAT**

Continue until additional passes stop finding **material, actionable improvements**.

Do not stop merely because the initial issues have been fixed.

---

# 21. DO NOT GAME THE BENCHMARK

Never:

* special-case known test prompts
* weaken evaluators to obtain better scores
* remove difficult examples
* lower acceptance thresholds simply to pass
* hide failed traces
* exclude agents because they are difficult to test
* replace true E2E tests with mocks and call it done

Mocks can supplement testing.

They do not replace real integration and E2E testing where those systems are available.

---

# 22. PROTECT BUSINESS BEHAVIOR

Optimization must not silently change required business behavior.

Before making significant architectural or semantic changes:

* determine the existing contract/spec/source of truth
* preserve required behavior
* create tests enforcing that behavior

You have authority to fix implementation defects and obvious inefficiencies.

Do not casually redesign business rules merely because a different implementation benchmarks better.

---

# 23. REQUIRED FINAL SCORECARD

At completion, produce a concise machine-generated scorecard containing all **24 agents**.

For each:

| Agent | Tests | Pass Rate | Correctness | Tool Use | Routing | Avg Steps | p95 Latency | Errors | Status |
| ----- | ----: | --------: | ----------: | -------: | ------: | --------: | ----------: | -----: | ------ |

Then provide system-wide:

* total test cases
* total experiment executions
* total Phoenix traces analyzed
* initial pass rate
* final pass rate
* routing accuracy
* tool-selection accuracy
* tool-argument accuracy
* schema compliance
* hallucination/faithfulness metrics
* failure rate
* retry rate
* average steps
* p95 latency
* token usage
* estimated cost where available
* regressions discovered
* regressions eliminated
* remaining known weaknesses

---

# 24. REQUIRED ARTIFACTS

Leave behind reusable infrastructure, not just results.

At minimum create/maintain:

* Phoenix instrumentation
* Genesis evaluation datasets
* deterministic evaluators
* LLM evaluators
* agent-specific tests
* routing tests
* tool-use tests
* adversarial tests
* failure-injection tests
* multi-agent E2E tests
* regression datasets
* experiment runners
* CI quality gates
* documented commands for rerunning everything
* concise test/optimization results

Put these in sensible repository locations and integrate them with the existing architecture rather than creating an isolated pile of scripts.

---

# 25. YOUR DEFINITION OF DONE

You may call this complete only when:

1. **All 24 Genesis agents are instrumented.**
2. **All 24 have meaningful Phoenix evaluation coverage.**
3. Individual-agent tests exist.
4. Routing is tested.
5. Tool selection/invocation is tested.
6. Multi-agent workflows are tested.
7. Adversarial scenarios are tested.
8. Failure recovery is tested.
9. Repeated/variance tests are run.
10. Performance is measured.
11. Token/cost inefficiencies are investigated.
12. Material defects discovered are fixed.
13. Each fixed defect becomes a regression test.
14. Baseline and optimized Phoenix experiments are compared.
15. Broad regression passes after optimization.
16. Critical quality gates are in CI where practical.
17. Remaining weaknesses are supported by evidence rather than speculation.
18. Phoenix contains useful traces and experiment results proving the conclusions.

---

# FINAL OPERATING RULE

**Do not come back to me simply with problems.**

If you encounter something that can reasonably be investigated, debugged, repaired, tested, refactored, instrumented, or optimized using the repository, environment, Phoenix traces, documentation, existing tools, or your own engineering judgment:

**DO IT.**

Do not ask me questions that the codebase, configuration, logs, Phoenix, tests, documentation, or experimentation can answer.

Find the answer.

Test the answer.

Implement the answer.

Measure the result.

Then test it again.

Your objective is not to tell me whether the 24 Genesis agents work.

Your objective is to **make them work as well as you can possibly make them, and produce the Phoenix evidence that proves it.**
