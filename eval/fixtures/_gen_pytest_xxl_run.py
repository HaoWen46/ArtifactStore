"""Deterministic generator for the 100K-token XXL CI-log fixture.

Run with `uv run python eval/fixtures/_gen_pytest_xxl_run.py` to
regenerate `pytest_xxl_run.log`. Same diagnostic `auth_expiry` bug as
`pytest_xl_run`, but with ~3× the surrounding CI noise. Designed to
test whether B4's input plateau holds at >>30K raw tokens — addresses
CRITIQUE §92.6 ("50K+ token fixtures").

Target: ~100K tokens (~400K chars). Same 8 failures including
auth_expiry, but a much longer captured-access-log tail (~85K tokens
of pure HTTP-request noise) and three repeated progress blocks that
mirror what a wide-test-tree project's monorepo CI run looks like.
Output committed; the generator is here so reviewers can audit
exactly how the fixture was built — no random noise, no
captured-from-production data.
"""
from __future__ import annotations
from pathlib import Path

# Re-use the XL components — same failures, same warnings, same
# captured-log block builder; we just turn up the volume.
from eval.fixtures._gen_pytest_xl_run import (
    HEADER, PROGRESS, EXTRA_PROGRESS,
    FAIL_REFUND, FAIL_AUTH, FAIL_MIGRATION, FAIL_QUERY,
    FAIL_PAGINATION, FAIL_INDEXER,
    EXTRA_FAIL_TIMEOUT, EXTRA_FAIL_WEBHOOK,
    WARNINGS, EXTRA_WARNINGS, BENCHMARK, SUMMARY, COVERAGE,
    STABILITY_TAIL, ENV_SUMMARY,
    _bulk_request_logs,
)


FIXTURE_PATH = Path(__file__).parent / "pytest_xxl_run.log"


# Three additional progress segments with rotating module names so the
# header/middle/tail of the progress section aren't all from the same
# subtree — closer to a monorepo CI shape, and forces the truncation
# baselines to discard later progress blocks that don't even name the
# offending module.
EXTRA_PROGRESS_TAIL = """\
tests/test_search/test_query_synonyms.py ......................................   [82%]
tests/test_search/test_geo_filters.py .........................................   [83%]
tests/test_search/test_sparse_vector.py ........................................  [84%]
tests/test_search/test_reranker.py ..............................................  [85%]
tests/test_billing/test_revenue_recognition.py ..............................     [86%]
tests/test_billing/test_credit_memos.py ....................................      [86%]
tests/test_observability/test_otel_sampling.py ............................        [87%]
tests/test_observability/test_otel_links.py .................................     [88%]
tests/test_observability/test_metrics_histograms.py .................             [88%]
tests/test_observability/test_log_sampling.py ............................        [89%]
tests/test_observability/test_log_destinations.py ......................          [89%]
tests/test_compliance/test_pii_redaction_pdf.py ........................          [90%]
tests/test_compliance/test_pii_redaction_csv.py .....................             [90%]
tests/test_compliance/test_consent_versioning.py ......................           [91%]
tests/test_compliance/test_retention_lifecycle.py ..........................      [91%]
tests/test_workers/test_jobqueue_priority.py ............................         [92%]
tests/test_workers/test_jobqueue_dlq.py ..............................            [92%]
tests/test_workers/test_jobqueue_visibility.py ......................             [93%]
tests/test_workers/test_jobqueue_retry.py ............................            [93%]
tests/test_admin/test_admin_audit_log.py .........................                [94%]
tests/test_admin/test_admin_export.py ........................                    [94%]
tests/test_admin/test_admin_user_search.py .................................      [95%]
tests/test_admin/test_admin_permissions.py ....................................   [95%]
tests/test_idp/test_jwt_rotation.py ........................................      [96%]
tests/test_idp/test_session_cookies.py ....................................       [96%]
tests/test_idp/test_oauth_state.py ............................................   [97%]
tests/test_idp/test_oauth_pkce.py .....................................           [97%]
tests/test_idp/test_oauth_dynamic_registration.py ...............................  [98%]
tests/test_idp/test_oauth_refresh.py ...................................          [98%]
tests/test_idp/test_openid_userinfo.py ..............................             [99%]
tests/test_idp/test_oidc_discovery.py .............................                [99%]
tests/test_idp/test_oidc_clientauth.py ...............................            [99%]
tests/test_idp/test_id_token_validation.py .........................              [99%]
"""


# Repeated structural footers — production CI runs sometimes interleave
# warnings/coverage/summary across stages. Deterministic, identical
# bytes each call.
INTERSTITIAL_WARNINGS = """\

================== additional pytest warnings tail ==================
tests/test_search/test_query_synonyms.py:42
  UserWarning: synonym table fallback to in-memory dict (Redis unreachable)
tests/test_observability/test_log_destinations.py:88
  UserWarning: stdout sink fallback because OTLP endpoint refused
tests/test_idp/test_oauth_pkce.py:124
  DeprecationWarning: PKCE code_challenge_method=plain is discouraged
tests/test_compliance/test_consent_versioning.py:67
  PendingDeprecationWarning: ConsentRecord(v1) will be removed in 3.0
tests/test_admin/test_admin_permissions.py:200
  ResourceWarning: unclosed permission-cache shard <Shard fd=42>
tests/test_workers/test_jobqueue_visibility.py:88
  RuntimeWarning: coroutine 'JobQueue.aack' was never awaited
"""


def build_fixture() -> str:
    return "".join([
        HEADER,
        PROGRESS,
        EXTRA_PROGRESS,
        EXTRA_PROGRESS_TAIL,
        FAIL_REFUND,
        FAIL_AUTH,                     # diagnostic target
        FAIL_MIGRATION,
        FAIL_QUERY,
        FAIL_PAGINATION,
        FAIL_INDEXER,
        EXTRA_FAIL_TIMEOUT,
        EXTRA_FAIL_WEBHOOK,
        WARNINGS,
        EXTRA_WARNINGS,
        INTERSTITIAL_WARNINGS,
        BENCHMARK,
        SUMMARY,
        COVERAGE,
        STABILITY_TAIL,
        # 600 requests × 6 log lines/request ≈ 80K tokens of pure noise.
        # Chosen to push total fixture size past the 100K-token mark
        # while keeping the diagnostic signal-to-noise ratio realistic
        # for a wide monorepo CI tail.
        _bulk_request_logs(num_requests=600),
        ENV_SUMMARY,
    ])


def main() -> None:
    text = build_fixture()
    FIXTURE_PATH.write_text(text)
    chars = len(text)
    approx_tokens = chars // 4
    print(f"wrote {FIXTURE_PATH}")
    print(f"  chars={chars} approx_tokens={approx_tokens}")


if __name__ == "__main__":
    main()
