# ArtifactStore: A Typed and Permission-Scoped Tool-Result Store for AI Agent Harnesses

## 0. One-line summary

Modern tool-using agents and subagents produce large intermediate outputs that are usually dumped into context, truncated, summarized, or hidden inside worker-agent summaries. **ArtifactStore** stores these outputs as typed, indexed artifacts and exposes compact, permission-scoped query views so supervisors and subagents can retrieve exact evidence under token and access constraints.

---

## 1. Motivation

Current AI agent systems increasingly use:

```text
skills
MCP tools
browser/computer tools
CLI execution
file search
code execution
subagents / handoffs
trace logging
context compaction
prompt caching
```

Progressive disclosure helps agents avoid loading every skill or tool schema at once. However, once an agent actually runs tools, it still creates huge intermediate outputs:

```text
pytest logs
npm test output
compiler errors
stack traces
grep/ripgrep results
browser snapshots
DOM dumps
API JSON responses
database query results
git diffs
security scan results
subagent research traces
```

These outputs are usually handled in one of four crude ways:

```text
1. dump the raw output into the transcript
2. truncate the output
3. summarize the output
4. hide the output inside a subagent and return only a summary
```

All four are flawed.

Raw dumping wastes tokens. Truncation loses evidence. Summary-only delegation can hallucinate or omit the exact failing line. Subagent isolation protects the parent context window, but also makes exact intermediate evidence harder to recover.

The key problem is therefore:

> Agent harnesses lack a typed, queryable, permission-scoped evidence layer for large tool results and subagent intermediate outputs.

ArtifactStore addresses this gap.

---

## 2. Core thesis

A tool result should not be treated as plain transcript text. It should be treated as a **materialized artifact** with:

```text
type
preview
evidence spans
raw payload hash
provenance
links to related artifacts
access-control metadata
lazy expansion operators
```

Instead of injecting this into context:

```text
[huge pytest log / browser snapshot / grep result / JSON response]
```

a tool returns this:

```json
{
  "artifact_id": "art_981",
  "type": "pytest_failure",
  "preview": "3 tests failed: test_auth_expiry, test_login, test_refresh",
  "key_spans": [
    "assertion line",
    "top stack frame",
    "changed file span"
  ],
  "available_views": ["preview", "evidence", "redacted", "raw"],
  "expand_ops": ["get_spans", "expand_view", "find_related"]
}
```

The prompt receives only the compact preview and artifact handle. Exact evidence remains recoverable through controlled queries.

---

## 3. Updated project framing

### Recommended title

**ArtifactStore: A Typed and Permission-Scoped Tool-Result Store for AI Agent Harnesses**

### Alternative title

**ArtifactStore: A Scoped Evidence Store for Tool-Using Multi-Agent Harnesses**

### Main contribution

ArtifactStore is a DB-backed evidence layer that reduces context pollution while preserving exact recoverability and enforcing least-privilege access for subagents.

### What this project is not

ArtifactStore is not a full agent framework, not a general memory system, not a replacement for MCP, and not a universal context optimizer.

It focuses on one painful object class:

```text
tool outputs and subagent-produced evidence
```

---

## 4. Why this is timely

The current agent paradigm is moving toward supervisor/worker decomposition. A main agent delegates subtasks to specialized subagents or tool-using workers. This reduces context pressure in the parent conversation, but creates a new problem:

```text
How does the worker get enough evidence to do its task,
without receiving the full parent transcript or unrestricted tool/result access?
```

The naive options are bad:

```text
summary-only delegation    -> loses exact evidence
full-context delegation    -> wastes tokens and leaks unrelated context
unrestricted shared store  -> permission mess
manual copy-paste evidence -> brittle and unstructured
```

ArtifactStore provides a middle path:

```text
supervisor grants scoped access to relevant artifacts
subagent queries only allowed views
subagent returns answer with artifact citations
supervisor can lazily verify exact evidence
```

This is the project’s strongest 2026 framing.

---

## 5. Supervisor/subagent use case

### Example task

A supervisor agent is debugging a failing authentication test. It runs `pytest`, producing a large log. Instead of dumping the whole log into context, the harness stores the output in ArtifactStore.

The supervisor then delegates:

```text
Task: Diagnose why the auth expiry test fails.
Allowed evidence: pytest failures, auth-related source spans, git diff.
Disallowed evidence: unrelated files, secrets, raw environment output.
```

The supervisor creates a scoped grant:

```json
{
  "grant_id": "grant_auth_debug_17",
  "subject_agent_id": "debug_worker_auth",
  "allowed_artifact_types": ["pytest_failure", "source_span", "git_diff"],
  "allowed_views": ["preview", "evidence"],
  "allowed_ops": ["search", "get_preview", "get_spans", "find_related"],
  "raw_access": false,
  "max_tokens": 2500,
  "expires_at": "2026-05-06T18:30:00+08:00"
}
```

The subagent can then query:

```python
artifact.search(
    query="auth expiry assertion",
    grant="grant_auth_debug_17",
    limit=5,
    token_budget=800
)

artifact.get_spans(
    artifact_id="art_981",
    span_type=["assertion", "stack_frame"],
    grant="grant_auth_debug_17"
)
```

The subagent returns:

```text
The failure appears to come from comparing token expiry using local time instead of UTC.
Evidence: art_981/span_7 contains the failed assertion, and art_1042/span_3 contains the changed expiry logic.
```

The parent agent does not need the full log unless it explicitly expands the artifact.

---

## 6. System design

```text
┌────────────────────────────┐
│ Supervisor Agent            │
└─────────────┬──────────────┘
              │ creates task + grant
              ▼
┌────────────────────────────┐
│ Subagent / Worker Agent     │
└─────────────┬──────────────┘
              │ scoped artifact queries
              ▼
┌────────────────────────────┐
│ ArtifactStore API           │
│ search / get_spans / expand │
│ grant / audit / link        │
└─────────────┬──────────────┘
              ▼
┌────────────────────────────┐
│ SQLite / DuckDB             │
│ artifacts, spans, links,    │
│ grants, access logs, FTS    │
└────────────────────────────┘
```

ArtifactStore sits inside the harness. It does not replace MCP or the agent runtime. It manages the outputs produced by tools and agents.

---

## 7. Data model

### 7.1 `artifacts`

Stores one tool result or subagent-produced evidence object.

```sql
CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  parent_artifact_id TEXT,
  creator_agent_id TEXT,
  tool_name TEXT,
  artifact_type TEXT NOT NULL,
  raw_uri TEXT,
  raw_hash TEXT NOT NULL,
  token_count INTEGER,
  preview TEXT,
  sensitivity_label TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Example artifact types:

```text
pytest_failure
compiler_error
grep_result
git_diff
browser_snapshot
api_json
db_query_result
source_span
subagent_report
```

### 7.2 `artifact_spans`

Stores extracted evidence slices from an artifact.

```sql
CREATE TABLE artifact_spans (
  span_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  span_type TEXT NOT NULL,
  file_path TEXT,
  line_start INTEGER,
  line_end INTEGER,
  text TEXT NOT NULL,
  token_count INTEGER,
  importance REAL,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)
);
```

Example span types:

```text
assertion
stack_frame
error_message
changed_line
json_field
query_row
log_warning
security_finding
```

### 7.3 `artifact_links`

Stores provenance and relationships between artifacts.

```sql
CREATE TABLE artifact_links (
  src_artifact_id TEXT NOT NULL,
  dst_artifact_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  confidence REAL,
  PRIMARY KEY (src_artifact_id, dst_artifact_id, relation)
);
```

Example relations:

```text
same_failure_after_patch
caused_by
derived_from
summarizes
contains_evidence_for
supersedes
```

### 7.4 `artifact_grants`

Stores scoped subagent access capabilities.

```sql
CREATE TABLE artifact_grants (
  grant_id TEXT PRIMARY KEY,
  subject_agent_id TEXT NOT NULL,
  issuer_agent_id TEXT NOT NULL,
  artifact_predicate TEXT NOT NULL,
  allowed_ops TEXT NOT NULL,
  allowed_views TEXT NOT NULL,
  max_tokens INTEGER,
  expires_at TIMESTAMP
);
```

The `artifact_predicate` can be implemented simply as JSON filters in the prototype:

```json
{
  "session_id": "sess_42",
  "artifact_types": ["pytest_failure", "source_span", "git_diff"],
  "path_prefixes": ["app/auth", "tests/auth"],
  "sensitivity_max": "internal"
}
```

### 7.5 `artifact_access_log`

Audits artifact reads and blocked accesses.

```sql
CREATE TABLE artifact_access_log (
  access_id TEXT PRIMARY KEY,
  grant_id TEXT,
  subject_agent_id TEXT,
  artifact_id TEXT,
  operation TEXT,
  view TEXT,
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  result_token_count INTEGER,
  allowed BOOLEAN,
  denial_reason TEXT
);
```

### 7.6 FTS index

```sql
CREATE VIRTUAL TABLE artifact_fts USING fts5(
  artifact_id,
  artifact_type,
  preview,
  span_text,
  tool_name
);
```

---

## 8. Artifact views

ArtifactStore should not expose raw output by default. It should expose views.

```text
preview_view
  compact summary, artifact type, tool name, token count, timestamp

evidence_view
  selected key spans, file/line metadata, error messages, assertions

redacted_view
  raw-ish output with secrets, credentials, unrelated paths, or private fields removed

raw_view
  full original output, only available with explicit permission

provenance_view
  command/tool, arguments hash, creator agent, source session, parent artifacts
```

This is the DBMS core:

```text
projection
late materialization
access-control views
provenance
materialized results
multi-resolution storage
```

---

## 9. API sketch

```python
class ArtifactStore:
    def put_artifact(
        self,
        tool_name: str,
        artifact_type: str,
        raw_text: str,
        creator_agent_id: str,
        metadata: dict,
    ) -> str:
        ...

    def search(
        self,
        query: str,
        grant_id: str,
        artifact_types: list[str] | None = None,
        limit: int = 5,
        token_budget: int = 1000,
    ) -> list[dict]:
        ...

    def get_spans(
        self,
        artifact_id: str,
        grant_id: str,
        span_types: list[str] | None = None,
        token_budget: int = 1000,
    ) -> list[dict]:
        ...

    def expand_view(
        self,
        artifact_id: str,
        grant_id: str,
        view: str,
        token_budget: int,
    ) -> str:
        ...

    def create_grant(
        self,
        subject_agent_id: str,
        issuer_agent_id: str,
        artifact_predicate: dict,
        allowed_ops: list[str],
        allowed_views: list[str],
        max_tokens: int,
        ttl_seconds: int,
    ) -> str:
        ...

    def audit(self, grant_id: str) -> list[dict]:
        ...
```

---

## 10. Research questions

### RQ1: Token efficiency

Can ArtifactStore reduce prompt tokens compared with raw tool-output injection, truncation, and summary-only baselines?

Metrics:

```text
tokens injected
parent context tokens
subagent context tokens
tokens per successful task
```

### RQ2: Exact evidence recovery

Can ArtifactStore preserve exact evidence better than truncated or summary-only approaches?

Metrics:

```text
evidence recall
exact assertion recovery
exact stack-frame recovery
citation correctness
ability to recover raw output when permitted
```

### RQ3: Subagent usefulness

Can scoped ArtifactStore queries let subagents solve delegated tasks without receiving full parent context?

Metrics:

```text
subagent task success
parent task success
subagent evidence recall
number of follow-up queries
number of reruns avoided
```

### RQ4: Permission enforcement

Can scoped grants prevent subagents from accessing unrelated or sensitive artifacts?

Metrics:

```text
unauthorized access blocked
permission violation rate
scope overgrant rate
scope undergrant rate
false block rate
audit completeness
```

---

## 11. Evaluation plan

### 11.1 Single-agent tool-output evaluation

Compare four systems:

```text
B1: raw tool output in context
B2: truncated tool output
B3: summary-only tool output
B4: ArtifactStore preview + lazy expansion
```

Tasks:

```text
debug pytest failure
diagnose npm test output
inspect compiler errors
summarize docker logs
analyze API JSON error
compare before/after git diff
```

Metrics:

```text
tokens injected
task success
diagnosis accuracy
evidence recall
exact evidence recoverability
number of tool reruns
```

### 11.2 Supervisor/subagent evaluation

Compare three delegation strategies:

```text
D1: summary-only subagent
D2: full-context subagent
D3: scoped ArtifactStore subagent
```

Tasks:

```text
subagent diagnoses failing test logs
subagent inspects browser/DOM traces
subagent analyzes security scan output
subagent compares before/after command outputs
subagent extracts relevant source spans from noisy grep results
```

Metrics:

```text
parent context tokens
subagent context tokens
final task success
evidence citation correctness
exact evidence recovery
unauthorized access blocked
scope overgrant rate
scope undergrant rate
```

### 11.3 Permission stress tests

Adversarial cases:

```text
artifact contains secret-looking values
artifact contains prompt injection
subagent asks for disallowed artifact_id
subagent requests raw_view when only evidence_view is allowed
subagent follows a link to an artifact outside scope
subagent tries to cite an artifact it cannot access
```

Metrics:

```text
leakage rate
blocked unauthorized reads
blocked raw expansions
false positives
false negatives
audit-log completeness
```

---

## 12. Baselines and ablations

### Baselines

```text
raw transcript dumping
fixed truncation
LLM summary-only
FTS search over raw transcript
ArtifactStore without permissions
ArtifactStore with scoped grants
```

### Ablations

```text
without typed spans
without artifact links
without redacted/evidence views
without token budget
without access logs
without subagent grants
```

---

## 13. Implementation plan

### Prototype stack

```text
Python
SQLite or DuckDB
SQLite FTS5
JSON metadata columns
simple token estimator
pytest / npm / rg / git as tool-output generators
optional embedding index if time permits
```

### CLI prototype

```bash
artifactstore init
artifactstore put --tool pytest --type pytest_failure --file pytest.log
artifactstore search "auth expiry assertion" --grant grant_17
artifactstore spans art_981 --type stack_frame --grant grant_17
artifactstore expand art_981 --view evidence --grant grant_17
artifactstore grant --agent debug_worker --types pytest_failure,source_span --views preview,evidence --ttl 30m
artifactstore audit --grant grant_17
```

### Minimal build order

```text
1. artifact table + raw storage
2. preview extraction
3. span extraction for pytest / grep / git diff
4. FTS search
5. evidence/raw/preview views
6. grant checker
7. access audit log
8. supervisor/subagent simulation
9. evaluation scripts
```

---

## 14. Expected results

Reasonable expected outcomes:

```text
30-60% fewer prompt tokens than raw tool-output injection
higher exact evidence recovery than truncation or summary-only
fewer tool reruns because exact outputs remain recoverable
similar or better debugging accuracy compared with raw-output baseline
lower parent-context usage in supervisor/subagent workflows
near-zero unauthorized artifact reads under explicit grant checks
```

Avoid overclaiming. The goal is not to solve all agent memory or all context optimization. The goal is to show that typed, scoped, lazy artifact access is better than raw transcript dumping or summary-only delegation.

---

## 15. DBMS angle

ArtifactStore maps directly to database systems ideas:

```text
tool outputs as materialized results
typed artifact tables
span-level indexing
FTS over previews and evidence spans
multi-resolution views
late materialization
access-control views
row/column-level permission checks
provenance and lineage
audit logs
token-aware retrieval cost
```

This is why the project fits a database systems course. It does not merely use a database as a dump bucket; it asks what schema and query interface an agent harness should use to manage large intermediate results.

---

## 16. Relationship to the previous ArtifactStore idea

The original version was:

> Store large tool outputs outside the transcript and inject compact previews plus lazy handles.

The revised version is:

> Store large tool outputs and subagent intermediate evidence as typed, indexed, permission-scoped artifacts with queryable views, lazy expansion, provenance, and audit logs.

The new version keeps the original token-efficiency goal but adds the missing modern agent requirement: **safe evidence sharing between supervisors and subagents**.

---

## 17. What not to build

Do not expand ArtifactStore into a full ContextDB or agent OS.

Avoid adding:

```text
skill selection
tool selection
general context planning
answer cache
global memory eviction
multi-agent scheduling
A2A protocol implementation
real GPU KV-cache management
```

Those are valid research directions, but they will dilute this project.

ArtifactStore should stay focused on:

```text
tool outputs
evidence spans
lazy retrieval
permission-scoped subagent access
provenance
auditability
```

---

## 18. Recommended final abstract

Modern tool-using agents produce large intermediate outputs such as test logs, compiler errors, browser snapshots, API responses, and subagent research traces. Current harnesses usually dump these outputs into the transcript, truncate them, summarize them, or hide them inside worker-agent summaries, causing token waste, evidence loss, and unsafe context sharing. We propose ArtifactStore, a typed and permission-scoped tool-result store for AI agent harnesses. ArtifactStore stores tool outputs as indexed artifacts with previews, evidence spans, provenance links, and multi-resolution views. Supervisors can grant subagents scoped access to selected artifact views, allowing workers to retrieve exact evidence without receiving full parent context or unrestricted raw outputs. We evaluate ArtifactStore against raw-output, truncation, summary-only, and full-context delegation baselines on debugging and log-analysis workloads, measuring token usage, task success, exact evidence recovery, reruns avoided, and permission enforcement.

---

## 19. Best final version

Use this as the concise pitch:

> **ArtifactStore is a scoped evidence substrate for tool-using AI agents. It replaces transcript dumping with typed, indexed, permission-scoped artifacts, allowing supervisors and subagents to recover exact tool-result evidence under token and access constraints.**

That is the cleanest version.

---

## 20. Demo agent design

The evaluations in §11 require a real, runnable supervisor/subagent harness. We deliberately keep this **small** — the demo's job is to show ArtifactStore working end-to-end, not to advance agent-framework state of the art. Anything fancier than what is below dilutes the contribution.

### 20.1 Stack

```text
Anthropic Messages API           (Python SDK)
client-side tool use loop        (~150 LOC, in demo/agent.py)
no MCP, no async-everywhere
no router, no planner, no scratchpad memory beyond message history
```

Reference implementation studied: `anthropic/anthropic-quickstarts/agents` (MIT). We adapt the `Tool` dataclass + parallel `execute_tools` pattern. Notes in `notes/agent_design.md`.

### 20.2 Two-agent topology

```text
                ┌──────────────────────────┐
                │ Supervisor (Claude)      │
                │ tools:                   │
                │   run_workload(target)   │  ──► writes artifact via put_artifact
                │   create_grant(...)      │
                │   delegate(task, grant)  │  ──► spawns subagent loop
                │   expand_artifact(view)  │  ──► verifies citations
                └─────────────┬────────────┘
                              │ grant_id only (no transcript)
                              ▼
                ┌──────────────────────────┐
                │ Subagent (Claude)        │
                │ tools (gated by grant):  │
                │   artifact_search        │
                │   artifact_get_spans     │
                │   artifact_expand_view   │
                │   artifact_find_related  │
                │   submit_report          │  ──► terminates the loop
                └──────────────────────────┘
```

Hard rules:
- The subagent never receives the supervisor's message history. The only handle is `grant_id`.
- `grant_id` is **bound at tool construction time**, not exposed as a tool parameter. The model never sees it. The harness enforces scope, not the LLM.
- Every read flows through `ArtifactStore.*`, which writes to `artifact_access_log`. The audit log is the RQ4 measurement surface — do not bypass it for "performance".
- After the subagent returns, the supervisor verifies each citation by calling `expand_artifact` on the cited `artifact_id/span_id`. Unresolvable citations → report rejected.

### 20.3 Agent loop (canonical, per Anthropic docs)

```python
while True:
    resp = client.messages.create(model=..., system=..., tools=[...],
                                  messages=messages)
    messages.append({"role": "assistant", "content": resp.content})
    if resp.stop_reason != "tool_use":
        break
    results = [exec_tool(b) for b in resp.content if b.type == "tool_use"]
    messages.append({"role": "user", "content": results})  # tool_results FIRST
return resp
```

Pitfalls already encoded in `demo/agent.py`:
- `tool_result` blocks come first in the next user message; text after.
- Every `tool_use_id` gets a matching `tool_result` (use `is_error: true` on failure with a useful hint).
- `submit_report` is forced via `tool_choice` once the subagent has run more than N turns, so the loop terminates predictably for eval runs.

### 20.4 What runs where

`demo/` layout:

```text
demo/
  agent.py     # Tool, ModelConfig, Agent.run() — generic Claude loop
  tools.py     # subagent_tools(store, grant_id), supervisor_tools(...)
  prompts.py   # SUPERVISOR_SYSTEM, SUBAGENT_SYSTEM
  runner.py    # `python -m demo.runner --fixture pytest.log` end-to-end demo
```

The supervisor's `run_workload` is **stubbed for the demo**: it reads a fixture file (a real pytest log captured offline) instead of executing pytest live. This makes runs deterministic and removes the eval-time dependency on whatever toy project we test against.

### 20.5 Models

- Default: `claude-sonnet-4-5` for both roles (cheap, fast tool use).
- Stress runs: `claude-opus-4-7` supervisor, sonnet subagent — closer to a real supervisor/worker decomposition.
- Model id lives **only** in `ModelConfig`, never inlined. Eval scripts can sweep it.

### 20.6 Fixture corpora (for eval)

Each PLAN §11.1 / §11.2 task needs a deterministic input fixture stored under `eval/fixtures/`:

```text
eval/fixtures/
  pytest_auth_expiry.log
  npm_test_flake.log
  rg_grep_noise.txt
  git_diff_auth_refactor.diff
  docker_oom.log
  api_500_payload.json
```

These are captured once, checked in, and replayed. The eval driver (PLAN §11) instantiates four configurations (B1 raw, B2 truncated, B3 summary-only, B4 ArtifactStore) against the same fixture and the same task description, measures (tokens_injected, evidence_recall, task_success, blocked_reads), and writes results to `eval/runs/<timestamp>/`.

### 20.7 What this demo deliberately omits

```text
- multi-step planner / router
- tool selection over a giant catalog
- long-running agent supervision UI
- streaming responses
- MCP servers
- learned retrieval / embeddings (FTS5 only, unless §11 needs it)
- recursive subagents (one supervisor → one subagent is enough)
```

If a reviewer asks "why so simple?" — the answer is in §17. ArtifactStore is the contribution; the agent harness is just the test bench.
