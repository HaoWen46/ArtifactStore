"""Deterministic generator for the 30K-token XL CI-log fixture.

Run with `uv run python eval/fixtures/_gen_pytest_xl_run.py` to regenerate
`pytest_xl_run.log`. Same diagnostic auth_expiry bug as `pytest_ci_run`,
but with ~3× the surrounding CI noise — more test modules, more captured
logs per failure, longer post-run sections. Designed to test the
projected B1/B4 cost-crossover regime where B1's per-fixture input is
strictly larger than B4's plateau (~15K). Output committed; the
generator is here so reviewers can audit how the fixture was built
(no random noise, no captured-from-production data).

Target: ~30K tokens (~120K chars), ~6 failures including auth_expiry,
~1500 tests, large flaky-history and stability-summary tails.
"""
from __future__ import annotations
from pathlib import Path

# Re-use the components from the 10K generator where they already exist.
from eval.fixtures._gen_pytest_ci_run import (
    HEADER, PROGRESS,
    FAIL_REFUND, FAIL_AUTH, FAIL_MIGRATION, FAIL_QUERY,
    FAIL_PAGINATION, FAIL_INDEXER,
    WARNINGS, BENCHMARK, SUMMARY, COVERAGE, ENV_SUMMARY,
)


FIXTURE_PATH = Path(__file__).parent / "pytest_xl_run.log"


EXTRA_PROGRESS = """\

tests/test_billing/test_invoicing_v2.py ......................................... [  6%]
tests/test_billing/test_billing_periods.py .........................            [  8%]
tests/test_billing/test_proration_engine.py .......................             [ 10%]
tests/test_billing/test_taxes_jurisdiction.py ...............................   [ 12%]
tests/test_billing/test_refund_reasons.py .............................         [ 14%]
tests/test_billing/test_subscription_state.py .................................. [ 16%]
tests/test_billing/test_payment_intent.py ......................                [ 18%]
tests/test_billing/test_payment_method_validation.py ..........................   [ 20%]
tests/test_billing/test_discount_engine.py ..............................       [ 22%]
tests/test_billing/test_credit_notes.py .........................               [ 23%]
tests/test_billing/test_dunning_v2.py .............................             [ 25%]
tests/test_billing/test_reseller_split.py .............................         [ 27%]
tests/test_billing/test_currency_conversion.py ........................          [ 28%]
tests/test_billing/test_invoice_pdf.py ...........................              [ 30%]
tests/test_cache/test_pubsub_clustering.py .........................            [ 32%]
tests/test_cache/test_ttl_jitter.py ............................                [ 33%]
tests/test_cache/test_serialization.py ........................                 [ 34%]
tests/test_cache/test_eviction_lru.py .........................                 [ 36%]
tests/test_cache/test_eviction_lfu.py ........................                  [ 37%]
tests/test_cache/test_pipeline.py ................................              [ 39%]
tests/test_cache/test_blocking_pop.py .....................                     [ 40%]
tests/test_cache/test_keyspace_notifications.py ...........................     [ 42%]
tests/test_observability/test_otel_resource.py ..............................   [ 44%]
tests/test_observability/test_otel_baggage.py ..........................        [ 45%]
tests/test_observability/test_correlation_id.py ......................          [ 47%]
tests/test_observability/test_log_redaction.py ........................         [ 48%]
tests/test_observability/test_metrics_cardinality.py ........................   [ 50%]
tests/test_compliance/test_pii_classification.py .............................. [ 51%]
tests/test_compliance/test_export_request.py ..............................     [ 53%]
tests/test_compliance/test_export_eu.py ......................................  [ 55%]
tests/test_compliance/test_export_us_state.py ..............................    [ 56%]
tests/test_compliance/test_audit_retention.py ............................      [ 58%]
tests/test_compliance/test_anonymization.py ........................            [ 59%]
tests/test_compliance/test_legal_hold.py .........................              [ 60%]
tests/test_search/test_index_lifecycle.py ............................          [ 62%]
tests/test_search/test_query_rewriting.py ............................          [ 63%]
tests/test_search/test_query_filters.py ............................            [ 64%]
tests/test_search/test_facet_aggregation.py ..............................      [ 66%]
tests/test_search/test_relevance_tuning.py ..........................           [ 67%]
tests/test_search/test_completion_suggester.py ............................     [ 69%]
tests/test_search/test_reindex_safety.py .................                      [ 70%]
tests/test_workers/test_pdf_v2.py ..............................                [ 72%]
tests/test_workers/test_pdf_signature.py ...........................            [ 73%]
tests/test_workers/test_pdf_streaming.py ..........................             [ 75%]
tests/test_workers/test_export_zip.py .........................                 [ 76%]
tests/test_workers/test_export_csv.py ............................              [ 77%]
tests/test_workers/test_export_parquet.py .........................             [ 79%]
tests/test_workers/test_notifier_email.py ..............................        [ 80%]
tests/test_workers/test_notifier_sms.py ..........................              [ 82%]
tests/test_workers/test_notifier_webhook.py .............................       [ 83%]
tests/test_workers/test_dlq_replay.py ...........................               [ 85%]
tests/test_workers/test_scheduler.py ............................               [ 86%]
tests/test_api/test_admin_audit.py ........................                     [ 87%]
tests/test_api/test_admin_users.py ..........................                   [ 89%]
tests/test_api/test_admin_tenants.py .......................                    [ 90%]
tests/test_api/test_admin_features.py .....................                     [ 91%]
tests/test_api/test_webhook_signing.py ........................                 [ 93%]
tests/test_api/test_webhook_replay.py ............................              [ 94%]
tests/test_api/test_idempotency_v2.py ..........................                [ 95%]
tests/test_integration/test_checkout_flow.py ............                       [ 96%]
tests/test_integration/test_signup_flow_v2.py ..........                        [ 97%]
tests/test_integration/test_password_reset_flow.py ..........                   [ 98%]
tests/test_integration/test_oauth_link_flow.py ............                     [ 99%]
tests/test_integration/test_subscription_upgrade.py ...........                 [100%]
"""


# A few extra decoy failures to inflate the noise without adding new
# diagnostic-worthy bugs. Each one mimics the same style as the
# pytest_ci_run failures so it doesn't stand out structurally.
EXTRA_FAIL_TIMEOUT = """\

___________________________ test_export_csv_large_dataset ___________________________

self = <tests.test_workers.test_export_csv.TestExportCsvLarge object at 0x7f9a44c1e0>

    def test_export_csv_large_dataset(self, db_engine):
        # Build a 250K row export and stream to S3 in 50K chunks.
        seed_rows(db_engine, count=250_000)
        result = export_csv(filter={"tenant_id": "tnt_42"}, chunk_size=50_000)
>       assert result.duration_seconds < 30, "export exceeded SLA"
E       Failed: Timeout >60.0s (method='signal')
E       pytest-timeout: test stopped after 60.0s
E       AssertionError: export exceeded SLA — partial state at line 142_318

tests/test_workers/test_export_csv.py:284: Timeout
------------------------------ Captured log call -------------------------------
INFO     workers.export:csv.py:42 export starting for tenant_id=tnt_42 chunk_size=50000
INFO     workers.export:csv.py:55 streaming first chunk to s3://exports/tnt_42/csv/chunk-00.csv
DEBUG    workers.export:csv.py:68 chunk-00: 50000 rows in 5.42s; throughput=9.2k rps
DEBUG    workers.export:csv.py:68 chunk-01: 50000 rows in 6.18s; throughput=8.1k rps
WARNING  workers.export:csv.py:74 chunk-02: slow query (12.4s); index on tenant_id may be stale
DEBUG    workers.export:csv.py:68 chunk-02: 50000 rows in 12.41s; throughput=4.0k rps
WARNING  workers.export:csv.py:74 chunk-03: slow query (18.8s); pgstat suggests autovacuum lag
DEBUG    workers.export:csv.py:68 chunk-03: 50000 rows in 18.84s; throughput=2.7k rps
ERROR    workers.export:csv.py:88 chunk-04 aborted due to test timeout signal
INFO     workers.export:s3.py:142 partial upload state recorded: 200000/250000 rows
INFO     workers.metrics:metrics.py:18 incremented counter: exports.aborted{reason=timeout} +1
WARNING  workers.export:csv.py:96 partial export NOT cleaned up; manifest will mark incomplete
"""

EXTRA_FAIL_WEBHOOK = """\

___________________________ test_webhook_replay_idempotency ___________________________

self = <tests.test_api.test_webhook_replay.TestReplay object at 0x7fc02181e0>
client = <TestClient app=<FastAPI title='example-app'>>

    def test_webhook_replay_idempotency(self, client):
        # Send the same webhook 5 times; only one should produce side effects.
        payload = {"event_id": "evt_a1b2c3", "type": "invoice.created", "data": {...}}
        for _ in range(5):
            client.post("/webhooks/stripe", json=payload, headers={"X-Sig": "valid"})
        side_effects = audit_query(event_id="evt_a1b2c3")
>       assert len(side_effects) == 1, f"expected 1 side effect; got {len(side_effects)}"
E       AssertionError: expected 1 side effect; got 3
E       assert 3 == 1
E        +  where 3 = len([<AuditRow ...>, <AuditRow ...>, <AuditRow ...>])

tests/test_api/test_webhook_replay.py:198: AssertionError
------------------------------ Captured log call -------------------------------
INFO     api.webhooks:stripe.py:42 received webhook: event_id=evt_a1b2c3 type=invoice.created
DEBUG    api.webhooks:stripe.py:48 signature validation passed for evt_a1b2c3
INFO     api.webhooks:idempotency.py:24 idempotency_key=evt_a1b2c3 lookup: not found
INFO     api.webhooks:idempotency.py:32 idempotency_key=evt_a1b2c3 recorded; ttl=3600s
INFO     api.webhooks:handlers.py:88 dispatching invoice.created handler for evt_a1b2c3
INFO     api.webhooks:audit.py:42 audit row written: action=webhook.dispatched event=evt_a1b2c3
WARNING  api.webhooks:idempotency.py:54 idempotency lookup race: redis SET NX returned 0 but TTL=3600
WARNING  api.webhooks:idempotency.py:58 second dispatch occurred due to concurrent write
INFO     api.webhooks:handlers.py:88 dispatching invoice.created handler for evt_a1b2c3 (DUPLICATE)
INFO     api.webhooks:audit.py:42 audit row written: action=webhook.dispatched event=evt_a1b2c3
WARNING  api.webhooks:idempotency.py:54 idempotency lookup race: redis SET NX returned 0 but TTL=3600
WARNING  api.webhooks:idempotency.py:58 third dispatch occurred due to concurrent write
INFO     api.webhooks:handlers.py:88 dispatching invoice.created handler for evt_a1b2c3 (DUPLICATE)
INFO     api.webhooks:audit.py:42 audit row written: action=webhook.dispatched event=evt_a1b2c3
DEBUG    api.webhooks:idempotency.py:62 final state: 3 dispatches recorded for evt_a1b2c3
"""


# A long "stability summary" tail: per-test-module pass-rate history,
# flake correlations, slowest-test rankings, etc. Pure noise for the
# diagnostic task.
STABILITY_TAIL = """\

============================ stability summary (14-day window) ============================
module                                    runs   pass   fail   p99_dur   flake_score
tests/test_billing/test_charges            14    14     0      4.2s      0.00
tests/test_billing/test_invoices           14    14     0      3.8s      0.00
tests/test_billing/test_refunds            14    2      12     5.1s      0.86
tests/test_billing/test_subscriptions      14    14     0      6.4s      0.00
tests/test_billing/test_proration          14    14     0      2.9s      0.00
tests/test_billing/test_tax                14    14     0      3.2s      0.00
tests/test_billing/test_payment_methods    14    14     0      4.8s      0.00
tests/test_billing/test_discounts          14    14     0      3.1s      0.00
tests/test_billing/test_credits            14    14     0      2.7s      0.00
tests/test_billing/test_dunning            14    14     0      3.8s      0.00
tests/test_billing/test_invoicing_v2       14    14     0      5.2s      0.00
tests/test_billing/test_billing_periods    14    14     0      3.9s      0.00
tests/test_billing/test_proration_engine   14    14     0      4.1s      0.00
tests/test_billing/test_taxes_jurisdiction 14    14     0      4.4s      0.00
tests/test_billing/test_refund_reasons     14    14     0      3.6s      0.00
tests/test_cache/test_evict                14    14     0      1.8s      0.00
tests/test_cache/test_get                  14    14     0      0.4s      0.00
tests/test_cache/test_set                  14    14     0      0.5s      0.00
tests/test_cache/test_ttl                  14    14     0      0.9s      0.00
tests/test_cache/test_invalidation         14    14     0      1.4s      0.00
tests/test_cache/test_cluster_failover     14    14     0      8.2s      0.00
tests/test_cache/test_pubsub               14    14     0      2.1s      0.00
tests/test_cache/test_pubsub_clustering    14    14     0      3.4s      0.00
tests/test_cache/test_ttl_jitter           14    14     0      1.1s      0.00
tests/test_cache/test_serialization        14    14     0      1.7s      0.00
tests/test_login                           14    14     0      1.2s      0.00
tests/test_refresh                         14    14     0      0.9s      0.00
tests/test_auth                            14    13     1      0.8s      0.07   *** NEW REGRESSION 2026-05-07 ***
tests/test_session                         14    14     0      1.4s      0.00
tests/test_oauth                           14    14     0      2.2s      0.00
tests/test_passwordless                    14    14     0      1.8s      0.00
tests/test_mfa                             14    14     0      3.4s      0.00
tests/test_saml                            14    14     0      4.8s      0.00
tests/test_db/test_migrations              14    3      11     12.4s     0.79
tests/test_db/test_query                   14    0      14     4.8s      1.00
tests/test_db/test_pool                    14    14     0      1.1s      0.00
tests/test_db/test_transaction             14    14     0      2.4s      0.00
tests/test_db/test_replica                 14    14     0      3.2s      0.00
tests/test_db/test_failover                14    14     0      8.8s      0.00
tests/test_db/test_constraints             14    14     0      2.4s      0.00
tests/test_db/test_views                   14    14     0      1.8s      0.00
tests/test_api/test_health                 14    14     0      0.2s      0.00
tests/test_api/test_invoices_endpoint      14    7      7      2.4s      0.50
tests/test_api/test_users                  14    14     0      1.9s      0.00
tests/test_api/test_oauth_endpoint         14    14     0      2.1s      0.00
tests/test_api/test_billing_endpoint       14    14     0      2.4s      0.00
tests/test_api/test_webhooks               14    14     0      3.1s      0.00
tests/test_api/test_admin                  14    14     0      4.2s      0.00
tests/test_api/test_search                 14    14     0      2.6s      0.00
tests/test_api/test_ratelimit              14    14     0      1.4s      0.00
tests/test_api/test_idempotency            14    14     0      1.2s      0.00
tests/test_workers/test_email              14    14     0      2.4s      0.00
tests/test_workers/test_pdf                14    14     0      4.8s      0.00
tests/test_workers/test_export             14    14     0      8.2s      0.00
tests/test_workers/test_indexer            14    11     3      6.4s      0.21
tests/test_workers/test_notifier           14    14     0      2.1s      0.00

slowest 25 tests (median over 14 runs):
   42.18s tests/test_workers/test_export_csv::test_export_csv_large_dataset
   38.72s tests/test_workers/test_pdf_streaming::test_pdf_streaming_large_doc
   24.18s tests/test_db/test_migrations::test_migrate_0042_backfill
   18.42s tests/test_search/test_reindex_safety::test_reindex_safety_under_load
   12.84s tests/test_db/test_failover::test_db_failover_with_writes_in_flight
   11.94s tests/test_cache/test_cluster_failover::test_cluster_failover_kills_replica
   10.21s tests/test_integration/test_checkout_flow::test_checkout_complete
    9.87s tests/test_integration/test_subscription_upgrade::test_upgrade_with_proration
    8.84s tests/test_workers/test_dlq::test_dlq_replay_large_batch
    7.62s tests/test_api/test_admin_audit::test_admin_audit_export
    7.28s tests/test_workers/test_scheduler::test_scheduler_concurrent_jobs
    6.84s tests/test_workers/test_export_zip::test_export_zip_large_archive
    6.42s tests/test_search/test_facet_aggregation::test_facet_aggregation_deep
    6.18s tests/test_db/test_transaction::test_serializable_isolation_under_load
    5.84s tests/test_billing/test_invoice_pdf::test_invoice_pdf_render_large
    5.42s tests/test_integration/test_password_reset_flow::test_password_reset_email_delivered
    5.18s tests/test_workers/test_notifier_email::test_notifier_email_batch_send
    4.94s tests/test_search/test_relevance_tuning::test_relevance_tuning_a_b_test
    4.62s tests/test_compliance/test_export_eu::test_export_eu_with_redaction
    4.18s tests/test_billing/test_billing_periods::test_billing_period_proration
    3.94s tests/test_observability/test_otel_resource::test_otel_resource_attributes_propagation
    3.62s tests/test_compliance/test_audit_retention::test_audit_retention_7_year_window
    3.18s tests/test_workers/test_pdf_signature::test_pdf_signature_chain_validation
    2.84s tests/test_workers/test_export_parquet::test_export_parquet_partitioned
    2.42s tests/test_api/test_idempotency_v2::test_idempotency_v2_redis_key_collisions

============================ failure correlation matrix ============================
                            refund   auth_exp  migrate  query    pagination  indexer
billing/test_refunds        1.00     0.00      0.00     0.00     0.00        0.00
auth/test_auth_expiry       0.00     1.00      0.00     0.00     0.00        0.00
db/test_migrations          0.00     0.00      1.00     0.42     0.00        0.00
db/test_query               0.00     0.00      0.42     1.00     0.18        0.00
api/test_invoices_pagin     0.00     0.00      0.00     0.18     1.00        0.12
workers/test_indexer        0.00     0.00      0.00     0.00     0.12        1.00

note: auth_expiry shows ZERO historical correlation with any other failing test —
suggests a discrete regression rather than a shared infra problem.

============================ build node info ============================
ci-runner-04: cpu=AMD EPYC 7763 64-Core, memory=128GiB, disk=NVMe RAID-0 2TB
kernel:       linux-6.8.4-aws #34-Ubuntu SMP PREEMPT_DYNAMIC
container:    ci-runtime:2026.05.07@sha256:8f7e3d2a1c4b5a6e0d9c8b7a6e5d4c3b2a1f0e9d
artifacts:    s3://ci-artifacts/builds/2026-05-07-39482/
log volume:   38.4MB compressed, 412MB uncompressed
duration:     27m42s wall clock; 142m9s sum-of-workers (8x parallelism)
"""


# Additional WARNINGS section with more deprecation/lint chatter — more
# noise that B2/B3 truncation can't avoid.
EXTRA_WARNINGS = """\

============================ extended pytest-warnings ============================
config.py:122
  PytestConfigWarning: --strict-markers is required as of pytest 8.0; will be default in 9.0

tests/test_billing/test_refunds.py:142
  DeprecationWarning: Refund.cents is deprecated; use Refund.amount_cents instead
  refund_value = refund.cents  # noqa: PD901

tests/test_db/test_query.py:188
  SAWarning: Coercing Subquery object into a select() for use in IN(); use .scalar_subquery()

tests/test_db/test_migrations.py:88
  RemovedIn21Warning: SQLAlchemy 2.1 will remove sqlalchemy.orm.relationship.cascade_backrefs

tests/test_api/test_webhooks.py:84
  PydanticDeprecatedSince20: `Config` class is deprecated. Use ConfigDict instead.

tests/test_workers/test_pdf.py:122
  DeprecationWarning: PIL.Image.LANCZOS is deprecated; use Image.Resampling.LANCZOS

tests/test_compliance/test_pii.py:42
  UserWarning: PII regex profile 'v3' is using legacy rules; v4 is recommended

tests/test_search/test_query_filters.py:88
  ElasticsearchDeprecationWarning: types are deprecated in mappings (since 7.x)

tests/test_workers/test_scheduler.py:124
  RuntimeWarning: coroutine 'AsyncScheduler._tick' was never awaited

tests/test_observability/test_otel_resource.py:42
  OTelDeprecationWarning: setting Resource.service.namespace via env is preferred

tests/test_workers/test_dlq.py:142
  PendingDeprecationWarning: rabbitmq.consume callback signature will change in v2.0

tests/test_billing/test_proration_engine.py:88
  FutureWarning: Decimal('-0.005').quantize() rounds half-even by default; explicit is better

tests/test_api/test_admin_audit.py:188
  SADeprecationWarning: Querying engine.execute() is deprecated since 2.0

============================ async test warnings ============================
RuntimeWarning: coroutine 'TestClient.aget' was never awaited (test_api/test_admin.py:88)
RuntimeWarning: coroutine 'AsyncSession.commit' was never awaited (test_db/test_pool.py:144)
RuntimeWarning: coroutine 'OpenSearchClient.aclose' was never awaited (test_search/test_query_rewriting.py:42)
RuntimeWarning: coroutine 'EmailService.aenqueue' was never awaited (test_workers/test_notifier_email.py:88)

============================ thread safety warnings ============================
ResourceWarning: unclosed thread <Thread(BatchProcessor, started 140123456)>
ResourceWarning: unclosed file <_io.BufferedReader name='/tmp/pytest-of-runner/large-fixture.csv'>
ResourceWarning: unclosed socket <socket.socket fd=58>
ResourceWarning: unclosed event loop <_UnixSelectorEventLoop running=True>
"""


def _bulk_request_logs(num_requests: int = 240) -> str:
    """Deterministic noise: a captured-logs replay of `num_requests` HTTP
    requests with synthetic but realistic-looking trace lines. Each
    request produces ~6 log lines. This is the kind of pure-noise tail
    a real CI run produces and which truncation/summary baselines should
    have no chance of digesting."""
    lines: list[str] = ["", "================== captured access-log tail =================="]
    for i in range(num_requests):
        # Cycle through a small fixed set of paths/users — strictly deterministic.
        path = ["/api/v1/invoices", "/api/v1/users", "/api/v1/admin/audit",
                "/api/v1/oauth/token", "/api/v1/webhooks/stripe",
                "/api/v1/search/invoices", "/api/v1/ratelimit/check"][i % 7]
        user = ["cus_42", "cus_88", "cus_127", "cus_201", "cus_348"][i % 5]
        ms = 12 + (i * 7) % 250
        status = [200, 200, 200, 200, 201, 204, 429, 200, 200, 200][i % 10]
        req_id = f"req_{(i * 12_345) & 0xffff_ffff:08x}"
        trace = f"trace_{(i * 67_891) & 0xffff_ffff:08x}"
        lines.append(
            f"INFO     app.middleware:request_id.py:24 {req_id} {trace} "
            f"path={path} user={user} status={status} duration_ms={ms}"
        )
        lines.append(
            f"DEBUG    app.middleware:auth.py:88 {req_id} jwt verified scope=read:invoices"
        )
        lines.append(
            f"DEBUG    app.middleware:ratelimit.py:54 {req_id} bucket={user}:tier_pro "
            f"remaining={87 - (i % 87)} reset_in=37s"
        )
        lines.append(
            f"DEBUG    app.api:handler.py:42 {req_id} handler={path.split('/')[-1]}_get "
            f"db_queries=2 cache_hits=1"
        )
        lines.append(
            f"INFO     app.observability:audit.py:24 {req_id} action=api.read "
            f"actor={user} resource={path}"
        )
        lines.append(
            f"DEBUG    app.observability:tracing.py:67 span {trace} closed: kind=server "
            f"status_code=ok duration_ms={ms}"
        )
    return "\n".join(lines) + "\n"


def build_fixture() -> str:
    return "".join([
        HEADER,
        PROGRESS,
        EXTRA_PROGRESS,
        FAIL_REFUND,
        FAIL_AUTH,                     # diagnostic target
        FAIL_MIGRATION,
        FAIL_QUERY,
        FAIL_PAGINATION,
        FAIL_INDEXER,
        EXTRA_FAIL_TIMEOUT,            # noise failure
        EXTRA_FAIL_WEBHOOK,            # noise failure
        WARNINGS,
        EXTRA_WARNINGS,
        BENCHMARK,
        SUMMARY,
        COVERAGE,
        STABILITY_TAIL,                # 14-day flake history + slowest-tests
        _bulk_request_logs(num_requests=120),  # ~16K extra tokens of pure noise
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
