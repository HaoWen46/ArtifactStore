#set page(
  paper: "us-letter",
  margin: (x: 1in, y: 1in),
  numbering: "1",
)
#set par(justify: true, leading: 0.62em)
#set text(font: "New Computer Modern", size: 10.5pt)
#set heading(numbering: "1.1")
#show heading.where(level: 1): h => block(above: 1.2em, below: 0.6em)[
  #set text(size: 13pt, weight: "bold")
  #h
]
#show heading.where(level: 2): h => block(above: 0.9em, below: 0.4em)[
  #set text(size: 11.5pt, weight: "bold")
  #h
]
#show raw: r => box(fill: rgb("#f5f5f5"), inset: (x: 3pt, y: 1pt), outset: (y: 2pt), radius: 2pt)[
  #set text(font: "PT Mono", size: 9pt)
  #r
]
#show raw.where(block: true): r => block(
  fill: rgb("#f8f8f8"),
  inset: 8pt,
  radius: 4pt,
  width: 100%,
  stroke: 0.5pt + rgb("#dddddd"),
)[
  #set text(font: "PT Mono", size: 9pt)
  #r
]

#align(center)[
  #v(0.5em)
  #text(size: 18pt, weight: "bold")[
    ArtifactStore: A Typed and Permission-Scoped\
    Tool-Result Store for AI Agent Harnesses
  ]
  #v(0.3em)
  #text(size: 11pt)[
    System architecture report — DBMS course prototype
  ]
  #v(0.2em)
  #text(size: 10pt, style: "italic")[
    May 2026
  ]
  #v(0.5em)
]

= Abstract

Modern tool-using AI agents produce large intermediate outputs — pytest logs,
ripgrep results, browser snapshots, API JSON, subagent research traces — that
are typically dumped into the conversation transcript, truncated, summarized,
or hidden behind a worker-agent's summary. All four strategies are flawed:
raw dumping wastes tokens, truncation loses evidence, summary-only delegation
hallucinates, and worker isolation makes exact intermediate evidence
unrecoverable.

We propose `ArtifactStore`, a DB-backed evidence layer with a typed,
permission-scoped query interface. Tool outputs become indexed artifacts with
typed evidence spans, multi-resolution views, provenance links, and
audit-logged grants. A supervisor agent injects only an artifact handle into
its transcript and delegates inspection to a subagent under a scoped grant;
the subagent retrieves exact evidence through controlled tool calls and
returns a report with formal citations the supervisor can verify.

Implementation: ~2.0 kLOC of Python on SQLite + FTS5, plus a ~150 LOC
client-side tool-use loop. The same harness runs against Anthropic-native and
Anthropic-API-compatible endpoints (DeepSeek V4 Pro by default; ~10× cheaper
at parity behavior). 112 tests pass. Live evaluation: 84 runs across two
sweeps — §11.1 single-agent (4 fixtures × 4 baselines × 3 reps) shows
ArtifactStore (B4) is the only baseline with 100% task success and EER=1.00
on every fixture; §11.2 supervisor↔subagent (4 fixtures × 3 strategies × 3
reps) shows D3 (scoped ArtifactStore delegation) cuts parent context by 43%
on 3.5K-token fixtures and is the only strategy producing formal citations
and audit-log signal. §11.3 adversarial stress (10 offline scenarios) shows
zero unauthorized reads succeed.

= Motivation and Thesis

Today's agent harnesses lack a typed, queryable, permission-scoped evidence
layer for large tool results. The four canonical handling strategies all
fail in distinct ways:

#table(
  columns: (auto, 1fr, 1fr),
  inset: 6pt,
  stroke: 0.5pt + rgb("#bbbbbb"),
  align: (left, left, left),
  table.header(
    [*Strategy*], [*Failure mode*], [*Cost shape*]
  ),
  [Raw dump (B1)], [Doesn't scale; pollutes context],
    [Linear in fixture size],
  [Truncation (B2)], [Loses evidence past the cap],
    [Bounded but evidence-blind],
  [LLM summary (B3)], [Hallucinates or omits the decisive line],
    [+1 LLM call per artifact, plus quality variance],
  [Subagent isolation], [Exact evidence unrecoverable in parent],
    [Lower parent tokens; opaque audit],
)

The thesis: a tool result should not be plain transcript text — it should be
a *materialized artifact* with type, preview, evidence spans, raw payload
hash, provenance, links, access-control metadata, and lazy expansion
operators. A tool returns a compact handle; the agent retrieves exact
evidence through scoped queries when needed.

= System Overview

#figure(
  block(
    fill: rgb("#fafafa"),
    inset: 12pt,
    radius: 4pt,
    stroke: 0.5pt + rgb("#cccccc"),
    width: 100%,
  )[
    #set align(center)
    ```
                 ┌───────────────────────────────┐
                 │ Supervisor Agent              │
                 │   tools: run_workload         │ ─── put_artifact ──┐
                 │          create_grant         │                    │
                 │          delegate(task,grant) │ ─── creates ──────┐│
                 │          verify_citation      │                   ││
                 │          expand_artifact      │                   ││
                 └─────────────┬─────────────────┘                   ││
                               │ grant_id only                       ││
                               ▼                                     ││
                 ┌───────────────────────────────┐                   ││
                 │ Subagent (scoped to grant_id) │                   ││
                 │   tools: artifact_search      │                   ││
                 │          artifact_get_spans   │ ─── query ────┐   ││
                 │          artifact_expand_view │               │   ││
                 │          artifact_find_related│               │   ││
                 │          submit_report        │               │   ││
                 └───────────────────────────────┘               ▼   ▼▼
                                                       ┌────────────────────┐
                                                       │ ArtifactStore API  │
                                                       │ search/get_spans/  │
                                                       │ expand_view/...    │
                                                       │ create_grant/audit │
                                                       └─────────┬──────────┘
                                                                 ▼
                                                       ┌────────────────────┐
                                                       │ SQLite + FTS5      │
                                                       │   artifacts        │
                                                       │   artifact_spans   │
                                                       │   artifact_links   │
                                                       │   artifact_grants  │
                                                       │   artifact_access_log │
                                                       │   artifact_fts     │
                                                       └────────────────────┘
    ```
  ],
  caption: [Two-agent topology with the ArtifactStore boundary. The subagent
    never sees the supervisor's transcript — only a `grant_id` bound at
    tool-construction time. Every read flows through `ArtifactStore.*` and is
    audit-logged.],
)

== Hard invariants

The implementation enforces five invariants by construction:

1. *No raw output by default.* Under `ViewPolicy.ARTIFACT`, `run_workload`
   stores raw bytes in the store and returns only a handle (`artifact_id`,
   `type`, `preview`). The supervisor never sees raw transcripts unless it
   explicitly calls `expand_view(view='raw')` under a grant that allows it.

2. *Every artifact has* type, preview, raw_hash (sha256), creator_agent_id,
   session_id. Schema constraints ensure this.

3. *Every read attempt is logged*, allowed or denied. The audit log is the
   measurement surface for permission enforcement (RQ4) and is queryable
   per-grant.

4. *Token budgets are enforced, not advisory.* `search`, `get_spans`, and
   `expand_view` all use omit-fits ordering: include whole-or-skip until the
   next item would overflow. Plus a *cumulative* grant budget (Section 4.4)
   that ticks down across reads.

5. *Span extraction is type-driven*, registered per `artifact_type`. New
   types = new extractor, no branching inside a god function.

= Data Model

The schema (`artifactstore/schema.sql`) is six tables: five domain tables
plus an FTS5 virtual table.

== artifacts

```sql
CREATE TABLE artifacts (
  artifact_id        TEXT PRIMARY KEY,        -- art_<8hex>
  session_id         TEXT NOT NULL,
  parent_artifact_id TEXT,
  creator_agent_id   TEXT,
  tool_name          TEXT,
  artifact_type      TEXT NOT NULL,
  raw_uri            TEXT,                     -- file-backed escape hatch
  raw_blob           TEXT,                     -- canonical raw storage
  raw_hash           TEXT NOT NULL,            -- sha256 hex
  token_count        INTEGER,
  preview            TEXT,
  sensitivity_label  TEXT DEFAULT 'internal',  -- public<internal<restricted<secret
  metadata_json      TEXT,
  created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

`raw_blob` holds the canonical raw text. The `raw_uri` column is reserved
for a future file-backed escape hatch; not used in the prototype.

== artifact_spans

```sql
CREATE TABLE artifact_spans (
  span_id      TEXT PRIMARY KEY,           -- span_<8hex>
  artifact_id  TEXT NOT NULL,
  span_type    TEXT NOT NULL,              -- assertion|stack_frame|...
  file_path    TEXT,
  line_start   INTEGER,
  line_end     INTEGER,
  text         TEXT NOT NULL,
  token_count  INTEGER,
  importance   REAL,                        -- [0,1], higher = more diagnostic
  FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)
);
```

Spans are extracted at write time by a type-driven registry
(`artifactstore/extractors.py`). For `pytest_failure`, three regex passes
yield assertion lines (`importance=0.95`), error messages (`0.9`),
top-stack-frame (`0.85`), and captured log warnings (`0.75`). For
`grep_result`, each `path:line:text` row becomes a span with importance
lowered to `0.4` if it matches `\b(TODO|FIXME)\b` (noise heuristic).
For `git_diff`, each `+`/`-` hunk line becomes a `changed_line` span.

== artifact_links

```sql
CREATE TABLE artifact_links (
  src_artifact_id TEXT NOT NULL,
  dst_artifact_id TEXT NOT NULL,
  relation        TEXT NOT NULL,    -- caused_by|derived_from|...
  confidence      REAL,
  PRIMARY KEY (src_artifact_id, dst_artifact_id, relation)
);
```

`put_artifact(parent_artifact_id=...)` automatically writes a
`derived_from` row, so every multi-step workload chain (pytest → git diff →
rerun) is traversable via `find_related` without callers needing to manage
the link table by hand. Other relations (`caused_by`,
`contains_evidence_for`) can be inserted by callers when they have the
domain knowledge to assert them.

== artifact_grants

```sql
CREATE TABLE artifact_grants (
  grant_id           TEXT PRIMARY KEY,
  subject_agent_id   TEXT NOT NULL,
  issuer_agent_id    TEXT NOT NULL,
  artifact_predicate TEXT NOT NULL,    -- JSON
  allowed_ops        TEXT NOT NULL,    -- JSON array
  allowed_views      TEXT NOT NULL,    -- JSON array
  max_tokens         INTEGER,           -- NULL = unlimited
  consumed_tokens    INTEGER DEFAULT 0, -- ticks up on each successful read
  expires_at         TIMESTAMP
);
```

Predicate is a JSON object with these axes (all optional, missing = no
constraint):

#table(
  columns: (auto, 1fr),
  inset: 5pt,
  stroke: 0.5pt + rgb("#cccccc"),
  align: (left, left),
  [`session_id`], [equality],
  [`artifact_types`], [array, set membership on `artifact_type`],
  [`sensitivity_max`], [string, evaluated as
    `SENSITIVITY[label] ≤ SENSITIVITY[max]`],
  [`path_prefixes`], [array, applied to `artifact_spans.file_path` at
    span-read time only — artifact-level reads are path-opaque],
)

`consumed_tokens` is the cumulative-budget counter (Section 4.4).
`expires_at` is enforced as a UTC instant; expired grants raise
`AccessDenied('grant expired')` with audit logging.

A synthetic `__supervisor__` grant is seeded by `migrate()` with all ops,
all views, no predicate, and no budget — used by the supervisor for its
own citation-verification calls so the audit-log foreign key always
resolves and there's no special case in app code.

== artifact_access_log

```sql
CREATE TABLE artifact_access_log (
  access_id          TEXT PRIMARY KEY,
  grant_id           TEXT,
  subject_agent_id   TEXT,
  artifact_id        TEXT,
  operation          TEXT,           -- search|get_spans|expand_view|...
  view               TEXT,           -- preview|evidence|redacted|raw|provenance
  timestamp          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  result_token_count INTEGER,
  allowed            BOOLEAN,
  denial_reason      TEXT
);
```

Every access — allowed or denied — writes one row. The `denial_reason`
strings come from `grants.check()` and are deliberately specific
(`"op 'expand_view' not in allowed_ops"`,
`"view 'raw' not in allowed_views"`,
`"artifact does not match grant predicate"`,
`"grant expired"`,
`"grant budget exhausted (5234/5000 tokens)"`).
Denied attempts are the RQ4 measurement signal.

== artifact_fts (FTS5)

```sql
CREATE VIRTUAL TABLE artifact_fts USING fts5(
  artifact_id UNINDEXED, artifact_type, preview, span_text, tool_name
);
```

One FTS row per artifact, populated at `put_artifact` time with
`span_text = "\n".join(spans)`. Search uses bm25 ranking with a LIKE
fallback for malformed FTS5 queries.

= Refinements (System-Strengthening Pass)

After the first eval sweep exposed weak spots, three refinements were
landed to strengthen the contribution thesis. Each is small in code (~30
LOC) but materially changes what the system offers.

== Span-aware preview

*Before*: `preview` was the type-specific summary header followed by
head-of-raw lines, capped at 256 tokens. For pytest, the head was the
`platform darwin`, `collected N items`, and progress bars — almost no
diagnostic signal.

*After*: spans are extracted at write time *before* the preview is built.
The preview body inlines the top-importance spans (sorted DESC), one line
each: `· assertion@tests/test_auth.py:84  assert validate_token(token) is
True, "token expired prematurely"`. The model gets real diagnostic signal
in the preview itself; for many one-bug fixtures, no further round-trip
is needed.

This makes preview-only inspection close to as informative as the full
evidence view, at ≤256 tokens. Falls back to head-of-raw when no spans
exist (unknown artifact_type).

== Cumulative grant budget

*Before*: the `max_tokens` column on a grant existed but was never
enforced. A subagent could call `expand_view` 100 times without
consequence.

*After*: every successful read calls `account_consumption(conn, grant_id,
tokens)` alongside `log_access`. `grants.check()` denies further reads
once `consumed_tokens >= max_tokens` with reason
`"grant budget exhausted (X/Y tokens)"`. Per-grant counters are
independent — exhausting `grant_a` does not affect `grant_b`. Grants with
`max_tokens=NULL` (the seeded `__supervisor__` grant) are unlimited and
never trip the check.

This is a real DBMS quota: the supervisor mints a grant with a token
budget, and the subagent's reads tick it down. When exhausted, all
further attempts are denied and audit-logged. Combined with `expires_at`,
this gives capability-style time × volume bounding.

== Auto-derived-from links

*Before*: the `artifact_links` table existed and `find_related` was
implemented, but no code path populated links. `find_related` was dead
code.

*After*: `put_artifact(parent_artifact_id=...)` automatically writes a
`(src=child, dst=parent, relation='derived_from', confidence=1.0)` row
via `INSERT OR IGNORE`. `find_related` then surfaces the chain. Provenance
metadata (Section 6.5, `provenance` view) lists all outbound links from
an artifact.

= API Surface

`ArtifactStore` (in `artifactstore/store.py`) implements one write
method, four read methods, and two grant/audit utilities. Read methods
take `grant_id` as a keyword argument; the harness binds `grant_id` at
tool-construction time so the LLM never sees it.

```python
class ArtifactStore:
    def put_artifact(*, tool_name, artifact_type, raw_text,
                     creator_agent_id, session_id,
                     metadata=None, sensitivity_label='internal',
                     parent_artifact_id=None) -> str
    def search(query, *, grant_id, artifact_types=None,
               limit=5, token_budget=1000) -> list[dict]
    def get_spans(artifact_id, *, grant_id, span_types=None,
                  token_budget=1000) -> list[dict]
    def expand_view(artifact_id, *, grant_id, view, token_budget=1500) -> str
    def find_related(artifact_id, *, grant_id, relations=None) -> list[dict]
    def create_grant(*, subject_agent_id, issuer_agent_id,
                     artifact_predicate, allowed_ops, allowed_views,
                     max_tokens, ttl_seconds) -> str
    def audit(grant_id) -> list[dict]
```

== The five views

PLAN §8 specifies five views, each constructed by a function of signature
`(conn, artifact_id, token_budget, *, predicate=None) -> str`.

#table(
  columns: (auto, 1fr),
  inset: 5pt,
  stroke: 0.5pt + rgb("#cccccc"),
  align: (left, left),
  table.header([*View*], [*Content*]),
  [`preview`], [stored preview, omit-fits truncated],
  [`evidence`], [spans by importance DESC, formatted with span_id +
    location for citation; respects predicate `path_prefixes`],
  [`redacted`], [`raw_blob` with regex masks (JWTs,
    `secret|password|api_key=...` patterns, `sk-...` keys); omit-fits
    truncated],
  [`raw`], [`raw_blob` directly; omit-fits truncated. Typically gated
    behind grants that exclude `'raw'` from `allowed_views`],
  [`provenance`], [JSON dump of artifact metadata + outbound links + span
    count],
)

`expand_view` dispatches through `views.VIEWS[name]` and passes the
caller's grant predicate so span-level views (e.g. `evidence`) honor
`path_prefixes` filters.

== Grant check pipeline

`grants.check(conn, grant_id, artifact_id, op, view)` runs in a fixed
order; every failure mode logs a denial row before raising:

1. `load_grant`: unknown grant_id → `'unknown grant: ...'`
2. `op not in allowed_ops` → `"op '...' not in allowed_ops"`
3. `view not in allowed_views` → `"view '...' not in allowed_views"`
4. `expires_at < now` → `'grant expired'`
5. `consumed_tokens >= max_tokens` → `'grant budget exhausted (X/Y)'`
6. (artifact-scoped only) `artifact not found` → `'artifact ... not found'`
7. (artifact-scoped only) predicate mismatch →
   `'artifact does not match grant predicate'`

Returns the loaded grant dict on pass.

= Demo Harness (PLAN §20)

The agent loop is ~150 LOC of synchronous Python in `demo/agent.py`,
adapted from Anthropic's MIT-licensed quickstart. It implements the
canonical client-side tool-use loop with three additions:

- A hard `max_turns` cap (default 10) that guarantees termination. We
  cannot reliably force `submit_report` via `tool_choice`: DeepSeek's
  reasoning model rejects named `tool_choice` with HTTP 400 ("does not
  support this tool_choice"); even `tool_choice: any` lets the model
  pick freely. The cap is the actual safety net.
- A `force_terminator` *nudge* via `tool_choice: {type: any}` in the last
  two turns — best-effort, ignored gracefully if the provider rejects it.
- Token accounting that captures `cache_read_input_tokens` and
  `cache_creation_input_tokens` separately from `input_tokens`. Necessary
  because DeepSeek's prompt cache makes the bare `input_tokens` field
  rep-dependent: `tot = input_tokens + cache_read + cache_creation`.

The supervisor and subagent are both Anthropic-Messages-API clients. The
SDK is provider-agnostic: setting `ANTHROPIC_BASE_URL` to
`https://api.deepseek.com/anthropic` routes the same code through
DeepSeek's compatible endpoint. We use DeepSeek V4 Pro by default
(\$0.435/M input, \$0.87/M output, with cache reads at \$0.0036/M).
Anthropic's Sonnet 4.5 stays a one-env-var swap.

== System prompts

Two prompts (`demo/prompts.py`) encode the procedural contract. Both are
short — verbose prompts inflate every turn's input.

The supervisor's prompt is rule-based: "do NOT compensate for delegation
failures by re-running workloads in-line; verify every citation with
`verify_citation`; bias toward minimal context." The subagent's prompt
optimizes for early termination: "after 3-4 evidence calls, submit even
if uncertain. Excessive search is a failure mode." Both rules track
findings from live runs (Section 7).

== Citation verification

The subagent submits citations of the form `art_<8hex>/span_<8hex>`. The
supervisor calls `verify_citation(citation)` on each one;
`artifactstore.cite.verify_resolves` looks them up in `artifact_spans`.
Unresolvable citation → report invalid. Live demo run 3: 5 citations
submitted, 5 resolved, all logged via the seeded `__supervisor__` grant.

= Evaluation

PLAN §11.1 single-agent comparison: same fixture, same task, four
context-injection strategies. 4 fixtures × 4 baselines × 3 reps = 48
runs against `deepseek-v4-pro`. ~\$0.10. Output in
`eval/runs/<UTC-iso>/`.

== Aggregate (n=12 per baseline)

#table(
  columns: (auto, auto, auto, auto, auto, auto),
  inset: 5pt,
  stroke: 0.5pt + rgb("#cccccc"),
  align: (left, right, right, right, right, right),
  table.header(
    [*Baseline*], [*success*], [*avg_recall*], [*avg_tot_in*], [*avg_out*], [*avg_cost*],
  ),
  [B1 RAW],       [100%], [0.95], [1,341],  [1,343], [\$0.0015],
  [B2 TRUNCATED], [50%],  [0.43], [330],    [872],   [\$0.0008],
  [B3 SUMMARY],   [50%],  [0.45], [256],    [945],   [\$0.0009],
  [*B4 ARTIFACT*],[*100%*],[*0.94*],[*21,572*],[*3,288*],[*\$0.0051*],
)

B4 is the only baseline with both 100% task success and EER (exact
evidence recovery) = 1.00 across all four fixtures, and the only
baseline that produces formal citations (avg 4.3/run, all resolve).
B2/B3 fail in fixture-dependent ways: B2 loses anything past its
truncation cap; B3's heuristic summarizer preserves WARNING lines but
loses hunk-level diff structure.

== Where ArtifactStore wins

#list(
  marker: ([▸], [·]),
  spacing: 8pt,
  [*Reliable success across fixture types.* B1 and B4 are tied at 100%,
    but B1's input scales with fixture size while B4's stays bounded by
    the model's tool-use intent.],
  [*Exact evidence recovery.* B4 demonstrably reads the gold-truth
    must-contain strings via `get_spans`/`expand_view`. B1/B2/B3 ingest
    whole/truncated/summary payloads — EER is undefined for them.],
  [*Formal citations.* The supervisor calls `verify_citation` on each
    `art_xxx/span_yyy` citation; only B4 produces these, and only B4
    populates the `artifact_spans` table the verification looks up
    against.],
  [*Audit-log signal.* Every read writes one row; denials carry useful
    `denial_reason` strings. The narrow-grant demo run blocks 3
    unauthorized `view='raw'` attempts organically.],
)

== Where ArtifactStore is honest about its costs

PLAN §14 predicted 30–60% fewer prompt tokens for B4 versus raw injection.
The current sweep does *not* show that on absolute `tot_in` — B4 is ~16×
B1 because multi-turn loops accumulate context across turns. The fairer
framings:

- *Cost (with cache)*: at 3,500-token fixtures, B4 costs \$0.0032 vs B1's
  \$0.0025 — a 28% premium for strict evidence recovery and citation
  validity. The crossover with B1 happens around 5K-token fixtures.
- *Cost-per-success*: B2 and B3 are 50% success, so their effective
  cost-per-correct-diagnosis doubles. B4's \$0.0032 with 100% success
  beats B2's `\$0.0008/0% = ∞` and B3's `\$0.0005/0% = ∞` on the large
  fixture.
- *Headline framing*: ArtifactStore is *not* the cheapest baseline on
  these fixtures. It is the *only* baseline that combines reliable
  success + formal evidence citations + audit-log signal. On large or
  noisy fixtures (where B2/B3 strictly fail), it costs roughly the same
  as raw injection while being the only baseline a supervisor can trust.

== Supervisor↔subagent delegation (PLAN §11.2)

A second eval, fundamentally different question: not "what context
strategy works for a single agent" but "when delegating, what does the
*supervisor* keep in its context vs the subagent's." Three strategies,
36 runs, ~\$0.19:

#table(
  columns: (auto, auto, auto, auto, auto, auto, auto),
  inset: 5pt,
  stroke: 0.5pt + rgb("#cccccc"),
  align: (left, right, right, right, right, right, right),
  table.header(
    [*Strategy*], [*succ*], [*recall*], [*par_in*], [*sub_in*], [*tot_in*], [*cost*],
  ),
  [D1 SUMMARY],         [42%], [0.50], [3,065], [402],    [3,467],  [\$0.0025],
  [D2 FULL_CONTEXT],   [100%], [0.93], [6,010], [1,664],  [7,674],  [\$0.0042],
  [*D3 SCOPED*], [*100%*], [*0.92*], [*6,294*], [*27,137*], [*33,431*], [*\$0.0090*],
)

Headline §11.2 finding: *D3's parent-context savings are conditional on
fixture size*. On the `pytest_large_run` fixture (3,500 tok raw), D3
parent input = 6,708 vs D2's 11,716 — D3 cuts parent context by 43%. On
small fixtures (~few hundred tok raw), D3's parent is *larger* than D2's
because D3 makes 3 LLM calls (`run_workload` → `create_grant` →
`delegate`) vs D2's 2 calls, and per-turn overhead exceeds the
per-payload savings. *The crossover sits around 3-4K raw tokens.*

D3 is also the only strategy that produces formal citations (avg 6.4
per run, all resolve) and surfaces RQ4 signal organically (5 unauthorized
reads blocked across 12 D3 runs; 0 in D1/D2 because they have no
permission surface). The cost penalty over D2 is ~2× — buying formal
citations, audit signal, and bounded-parent-context.

Honest framing for §11.2: ArtifactStore is *not* always cheaper as a
delegation strategy. For small fixtures, raw forwarding (D2) is cheaper.
For large fixtures, ArtifactStore wins on parent-context, citations, and
audit simultaneously — the only strategy a supervisor can actually trust
when payloads grow.

== Adversarial permission stress (PLAN §11.3)

Ten offline stress tests (`tests/test_stress.py`, no API spend) cover
every PLAN §11.3 adversarial scenario plus related defenses:

#table(
  columns: (1.2fr, 1fr),
  inset: 5pt,
  stroke: 0.5pt + rgb("#cccccc"),
  align: (left, left),
  table.header([*Scenario*], [*Denial signal*]),
  [secret values + raw denied; redacted strips them],
    [`view 'raw' not in allowed_views`; redaction strips JWTs/secrets],
  [prompt injection cannot fabricate citations],
    [`cite.verify_resolves` rejects unknown span_ids],
  [out-of-session artifact_id requested],
    [`artifact does not match grant predicate`],
  [raw_view requested under evidence-only grant],
    [`view 'raw' not in allowed_views`],
  [following link to out-of-scope artifact],
    [link listed via find_related; expand_view denied on follow],
  [grant budget exhaustion under attack],
    [`grant budget exhausted (X/Y tokens)`],
  [path-prefix filter denies out-of-prefix spans],
    [filtered at render time in `evidence` view],
  [sensitivity ceiling blocks higher-labeled artifacts],
    [`artifact does not match grant predicate`],
  [expired grant denies all reads], [`grant expired`],
  [audit invariant: every denial has non-null reason], [aggregate over above],
)

*Result: zero unauthorized reads succeed across all 10 scenarios.* Every
denial is logged with a specific, parseable `denial_reason`. The §11.3
suite verifies the access-control surface as a unit-test contract;
§11.2's denials emerge organically from narrow grants in the live demo
flow. Both produce comparable signal via `grants.check()`.

== RQ4 summary across §11.2 + §11.3

For permission enforcement, the combined evidence is:

- §11.3 stress suite: 9 distinct attack vectors, all blocked, all logged
  (10 tests, 100% pass).
- §11.2 organic denials: 5 unauthorized reads attempted across 12 D3
  runs, all blocked, all logged.
- Demo runner with narrow grants: 3 raw-view attempts blocked in run 3.

Zero unauthorized reads succeed in any measurement surface.

= Provider-Agnostic Implementation

The agent loop uses the `anthropic` Python SDK as its HTTP client; the
SDK works against any Anthropic-API-compatible endpoint via the
`ANTHROPIC_BASE_URL` environment variable. There is no provider-specific
code in the harness.

Three pre-flight probes ship with the runner:

#table(
  columns: (auto, 1fr),
  inset: 5pt,
  stroke: 0.5pt + rgb("#cccccc"),
  align: (left, left),
  [`--check-config`], [Print resolved provider config (model, base_url,
    key presence). No network. Use before paid runs to verify `.env`.],
  [`--verify-tool-use`], [One paid call (~\$0.0001) that confirms the
    provider emits Anthropic-format `tool_use` blocks. Required before
    the eval driver.],
  [`--verify-tool-choice`], [Probe whether named/`any` `tool_choice` is
    honored. Reveals provider-specific quirks (e.g. DeepSeek's reasoning
    model rejects named `tool_choice` with 400).],
)

These three together catch every category of provider configuration error
we've observed in practice (wrong key, wrong base_url, model that doesn't
support the API shape, model that silently ignores constraints) before
spending eval budget.

= Testing and Reproducibility

112 tests pass against the prototype. Coverage:

- *Unit*: schema migration idempotency, ID format, predicate matching for
  every axis, sensitivity ordering, span-prefix path opacity, citation
  parsing + DB resolution, audit-log shape (allow + deny rows), token
  budget enforcement (decrement, exhaustion, supervisor unlimited,
  per-grant isolation), span-aware preview (inlines top span, falls back
  cleanly, respects token cap), auto-derived-from links (writes,
  no-parent path, idempotent), agent env-key safety, base_url propagation,
  max_turns cap, and tool_choice graceful fallback.
- *Integration*: span extractors against captured fixtures (pytest log,
  ripgrep output, git diff), all five view materializers against a real
  artifact, cite/verify round-trip against an inserted span.
- *Offline E2E*: full supervisor↔subagent flow via a `ScriptedClient`
  mocking the Anthropic SDK shape — no API key needed. Adversarial denial
  path (subagent attempts `view='raw'` under a grant that excludes it) is
  also exercised offline.
- *Live E2E*: `--verify-tool-use` and `--verify-tool-choice` are paid
  smoke tests; the demo runner produces real audit logs we compare against
  expected RQ4 signal manually.
- *Adversarial stress (PLAN §11.3)*: 10 tests in `tests/test_stress.py`
  driving deliberately-problematic situations (secrets in raw, prompt
  injection, out-of-session artifact requests, raw-under-evidence grant,
  out-of-scope link follow, budget exhaustion, path-prefix violations,
  sensitivity ceiling, expired grant). All 10 pass; zero unauthorized
  reads succeed.

The eval driver writes deterministic on-disk output: `config.json`,
`result.jsonl`, `audit.csv`, `manifest.json` per sweep. `git_rev` and
`base_url` are recorded in `config.json` so any sweep can be reproduced
verbatim from the commit + provider key.

= Out of Scope

PLAN §17 explicitly excludes: skill/tool selection, general agent memory,
KV-cache management, A2A protocol implementation, multi-agent scheduling,
global eviction. The harness deliberately uses only one supervisor and
one subagent — no recursion, no router, no planner. The Anthropic-style
prompt-caching multipliers are real (we observe them and account for
them) but we do not optimize cache breakpoints; the SDK's defaults
suffice for this prototype.

The contribution is *the typed, scoped evidence layer*. The agent harness
is a test bench. We deliberately keep it small so reviewers can read it
end-to-end in an hour.

= Open Issues and Future Work

#list(
  marker: ([▸], [·]),
  spacing: 6pt,
  [*Larger fixtures for a clean RQ1 win.* Current fixtures top out at
    3.5K tokens — past the B1/B4 crossover but not by much. A real
    10K-token CI log would let the eval show B1's input scaling vs B4's
    handle-bounded plateau.],
  [*Real B3 (LLM-summary) baseline.* The deterministic regex summarizer
    is a strawman; an LLM-summary B3 would be a fairer comparison and
    cost only ~\$0.001 per fixture-summary.],
  [*Supervisor↔subagent (D1/D2/D3) eval.* PLAN §11.2 — currently only
    the demo runner exercises delegation; folding it into the eval grid
    would round out the evaluation.],
  [*Variance reduction.* `temperature=0` deterministic mode would tighten
    error bars; current B4 grep ranges from 5 to 10 turns across reps.],
  [*Sensitivity inference.* All artifacts default to `internal`; a small
    heuristic (JWT-shape, `password=` patterns) bumping artifacts to
    `restricted` would make the redacted view do meaningful work and
    exercise the `sensitivity_max` predicate axis under realistic
    conditions.],
  [*Per-span audit attribution.* Currently `expand_view` logs one row per
    call; for span-level views, attributing the read to specific spans
    would give richer RQ4 signal.],
)

= Conclusion

ArtifactStore reframes tool outputs as typed, indexed, permission-scoped
artifacts with multi-resolution views. The implementation is a thin
SQLite + FTS5 layer (~2.0 kLOC) plus a small client-side agent loop.

Across 84 live runs against DeepSeek V4 Pro plus 10 offline stress
scenarios:

- *§11.1 single-agent*: ArtifactStore (B4) is the only configuration with
  both 100% diagnostic-task success and 100% exact-evidence recovery on
  every fixture, and the only baseline with formally verifiable citations.
- *§11.2 supervisor↔subagent*: ArtifactStore-scoped delegation (D3) is
  the only strategy with bounded supervisor context as fixtures grow
  (43% parent-context reduction at 3.5K-token fixtures vs full-context
  forwarding), the only strategy producing citations the supervisor can
  verify, and the only strategy with audit-log signal.
- *§11.3 adversarial stress*: zero unauthorized reads succeed across 9
  attack vectors. Every denial logs a specific `denial_reason`.

The contribution is not "this baseline saves the most tokens" — it is
"this is the only baseline a supervisor can trust." That distinction is
the right one for AI-agent harnesses moving toward supervisor/worker
decomposition. The permission-enforcement story (RQ4) reaches the
zero-unauthorized-reads bar across every measurement surface we built.

#v(0.5em)
#line(length: 100%)
#text(size: 9pt, style: "italic")[
  Source: #link("https://github.com/HaoWen46/ArtifactStore"). Reproduce
  the §11.1 sweep with `uv run python -m eval --reps 3` (~\$0.10), the
  §11.2 sweep with `uv run python -m eval --mode delegation --reps 3`
  (~\$0.19), and §11.3 with `uv run pytest tests/test_stress.py`
  (offline) after configuring `.env` per `.env.example`.
]
