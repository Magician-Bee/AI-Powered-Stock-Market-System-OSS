# Incremental Loader V2 / schema v18

Schema v18 completes `DATA-008` by recording each incremental update as an
auditable run and each committed source page as a batch:

| Table | Responsibility |
| --- | --- |
| `data_ingestion_checkpoints` | Latest committed cursor for one source, dataset and partition |
| `data_ingestion_runs` | Start cursor, final committed cursor, status and totals for one attempt |
| `data_ingestion_batches` | Input/output cursor and record count for every committed or failed page |

`IncrementalLoader.run` always passes the last committed cursor to the source
fetcher. A page is persisted before its output cursor becomes the new
checkpoint. Multi-page sources declare `metadata.has_more`; the loader continues
until the source is exhausted or `max_batches` creates a controlled `paused`
checkpoint. A later invocation starts from that cursor instead of downloading
the completed pages again.

Failures retain the cursor from the last successfully persisted page. The next
invocation therefore retries only the interrupted page. A source that declares
more pages without advancing its cursor is rejected to prevent an infinite
download loop. When a new attempt resumes a partition left `running` by a
process stop, the abandoned audit row becomes `interrupted` before the new run
starts. After a successful run, the normal dataset TTL returns `skipped_fresh`
without calling the source.

Because a process can terminate between persistence and checkpoint update,
page persistence remains at-least-once at the current page boundary. Immutable
payload hashes and revision identity make that retry idempotent; already
committed earlier pages are not revisited.

The status and audit APIs are:

```text
GET /api/data/status
GET /api/data/ingestion/runs
GET /api/data/ingestion/runs/{run_id}
```

The Data Catalog shows completed runs, committed batches and resumable
checkpoints.

## Verification

```bash
uv run pytest -q tests/test_unified_market_data_platform.py
uv run python scripts/verify_incremental_loader.py
```

The verifier processes three pages, pauses after page two, resumes from the
second committed cursor, and proves a fresh replay performs no fetch.
