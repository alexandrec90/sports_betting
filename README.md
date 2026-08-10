# Sports Betting

Sports research and data collection for Québec. Wager execution is intentionally out of
scope until a legally available operator explicitly permits API automation.

Generated from [devkit](https://github.com/alexandrec90/devkit)'s project
template. The agent harness in `scripts/hooks/` is vendored from there — see
`CLAUDE.md`, "Vendored agent harness".

## Quick start

```bash
cp .env.example .env          # then fill in the placeholders
uv sync --all-extras          # creates .venv from the committed uv.lock
# no uv? pip install -e ".[dev]" works, but resolves fresh instead of from the lock
docker compose up -d
pytest
```

Ingest yesterday's events/results with the no-signup TheSportsDB source:

```bash
uv run sports-betting ingest-events --sport "Ice Hockey"
uv run sports-betting ingest-events --date 2026-08-03 --league 4380
uv run sports-betting ingest-events --from 2026-08-01 --to 2026-08-03 --sport Soccer
```

Run every configured collector once, or keep the scheduler running:

```bash
uv run sports-betting collect
uv run sports-betting collect --provider football-data
uv run sports-betting serve
# persistent container (also starts with a plain `docker compose up -d`)
docker compose up -d collector
```

The scheduler serializes all writes and runs each provider every six hours. Missing-key jobs
are recorded as `skipped`, not failures. Outcomes live in `logs/scheduler-health.json`; the
restart-safe Odds API safety budget lives in `logs/provider-quotas.json`. Keep only one
collector instance running against an archive root.

Ask whether collection is actually working — the command exits non-zero when any job needs
attention, so it works as a container healthcheck or a cron guard:

```bash
uv run sports-betting health
uv run sports-betting health --quiet   # only the jobs that need attention
```

```
the-odds-api     failing    last ok 3d ago     wrote 3d ago     runs 12 fail 4
                 └─ basketball_nba: 401 Unauthorized …
```

Each job is classified from its recorded history rather than its last run alone:

| status | meaning |
| --- | --- |
| `ok` | collecting normally |
| `failing` | the last run errored; `consecutive_failures` says for how long |
| `stale` | no success in 2.5× the job's own interval — it stopped firing |
| `degraded` | it collected, but one configured sport failed (see `last_error`) |
| `idle` | succeeding but storing nothing for three runs — a 200 OK that collects zero |
| `skipped` | no API key configured; not counted as a run either way |
| `never-run` | scheduled but has not completed a run yet |

History survives restarts, so an open failure streak is not erased by bouncing the container,
and `last_success`/`last_wrote` are kept separately from the last run — an outage no longer
erases the record of when the job last worked.

The default local archive is `../data-lake/data/archive`. Each provider response becomes a
content-addressed snapshot in Hive-partitioned Parquet, so an unchanged re-fetch is a no-op
while a schedule later becoming a final result remains auditable. The catalog is written to
`_catalog/sports_events.json`. Override `ARCHIVE_ROOT` in `.env` when the lake lives elsewhere.
See [data sources](docs/data-sources.md) for provider trade-offs and the Québec constraint.

## Historical bulk imports

The recommended free training dumps need no credentials. Start with a narrow range, verify
disk use and schema, then expand it:

```bash
# Soccer match results, statistics, and historical bookmaker odds
uv run sports-betting bulk-import football-data --from-season 2020 --to-season 2025 --leagues E0,SP1

# NFL play-by-play (available from 1999)
uv run sports-betting bulk-import nflverse --from-season 2020 --to-season 2025

# NHL shot data (available from 2007) and compact team game-level history
uv run sports-betting bulk-import moneypuck --from-season 2020 --to-season 2025
uv run sports-betting bulk-import moneypuck-games

# Discover StatsBomb IDs, then import one competition-season's matches and events
uv run sports-betting statsbomb-list
uv run sports-betting bulk-import statsbomb --competition-id 9 --season-id 281
```

Each source file is retained exactly and converted to Zstandard-compressed Parquet under its
own dataset in `ARCHIVE_ROOT`. Catalogs contain source URLs, SHA-256 hashes, fetch times,
licence links, row counts, and schemas. Repeated imports use ETag/Last-Modified validators;
changed publisher files create immutable `version=<hash>` partitions.

Historical backfills are intentionally manual because a broad run can consume gigabytes.
Set `BULK_REFRESH_ENABLED=true` to add a serialized weekly current-season refresh; configure
its sources and StatsBomb IDs with the `BULK_*` variables documented in `.env.example`.

Budget both copies: every artifact keeps the publisher's original file *and* the Parquet.
StatsBomb is the outlier — its 4,235 event files are ~12 GB of uncompressed JSON, so always
bound it with `--max-matches`. Football-Data is ~50 MB, nflverse ~1.1 GB, MoneyPuck ~1.4 GB.

## Mirroring the archive off local disk

`ARCHIVE_ROOT` is the pooled bronze tree, but it is still local disk. `archive-sync` mirrors
it to an S3-compatible bucket (Cloudflare R2) or another directory, and can reclaim the space:

```bash
# The mirror needs the sibling data-lake checkout. It is not a declared dependency — see
# pyproject.toml for why a path source cannot be — so install it into .venv directly, and
# re-run this after any `uv sync`, which prunes whatever the lock does not name.
uv pip install -e ../data-lake[archive]

uv run sports-betting archive-sync --dry-run   # what would be uploaded, and what it would free
uv run sports-betting archive-sync             # upload + verify, delete nothing
uv run sports-betting archive-sync --prune     # ...then delete each verified source artifact
uv run sports-betting archive-restore          # pull pruned sources back down
```

Three properties are deliberate and should not be relaxed:

- **Nothing is deleted that has not been read back in the same run.** Verification fetches the
  uploaded object and matches its SHA-256, plus the row count for Parquet. A resumed `--prune`
  re-verifies objects an earlier run uploaded rather than trusting that they are intact.
- **Only *source* artifacts are pruned, never the Parquet.** The Parquet is what the next bulk
  import re-opens to rebuild a catalog, and it is the queryable copy. The source files are
  where the disk actually goes. StatsBomb's `statsbomb_matches`/`statsbomb_competitions` are
  additionally exempt because a later import reads them back to enumerate targets.
- **A pruned artifact stays discoverable.** Its provenance and catalog rows record
  `source_pruned` and the object key, so `BulkArchive.source_path` reports how to restore it
  instead of a bare "missing file".

For R2 the credentials are an **S3-compatible access key pair** from R2 → Manage API Tokens; a
general Cloudflare API token does not authenticate against the S3 endpoint. Region must be
`auto`. See `.env.example` for the full block.

> The store seam is bytes-in/bytes-out, so mirroring one file peaks at roughly twice its size
> (PUT body plus verification read-back). `archive-sync` refuses objects over
> `--max-object-mb` (256 MB default) and names them rather than risking an OOM in the
> 512 MB collector container. No streaming API is pending — it was considered upstream and
> declined on good grounds — but nothing here needs one: the largest artifact any configured
> source produces is ~20 MB, so the ceiling has ~10× headroom over the real worst case.

### Manifest shape

`_catalog/<dataset>.json` uses the sibling lake's `DatasetManifest` shape — `ts_column`, and
`updated_at` on every partition — because both projects write into one shared `_catalog/`
namespace. Manifests written before this was adopted use the old `timestamp_column` and are
unreadable by the lake; `sports-betting archive-recatalog` rewrites them in place (they are
derived from the Parquet, so it is safe to re-run).

In VS Code, `Ctrl+Shift+B` runs the default build task and the task quick-pick
(`Ctrl+Shift+P` → "Run Task") lists everything else, each with a one-line `detail`
explaining what it costs and what it touches.

## Host ports

This checkout is **slot 4** in devkit's `ports.toml`. Every published port
is its conventional base plus the slot:

| Service | Host port |
| --- | --- |
| postgres | 5436 |

Regenerate the `.env` block for any checkout with
`python <devkit>/scripts/devkit_ports.py <checkout-name>`.

## Parallel worktrees

A second checkout runs its own stack side by side:

```bash
git worktree add ../sports_betting-b sports_betting-b
cd ../sports_betting-b
cp ../sports_betting/.env .env    # then replace the ports block — slot 5
python <devkit>/scripts/devkit_ports.py sports_betting-b
docker compose up -d
```

`COMPOSE_PROJECT_NAME` must equal the directory name in each. See `CLAUDE.md`,
"Parallel worktrees", for the rules that keep the two stacks independent.

## Layout

```text
sports_betting/                  application code
tests/                tests
scripts/                 project scripts (Python, each with tests)
scripts/hooks/           vendored agent harness — edit upstream in devkit
.devkit.toml      the per-project harness seam (NOT vendored)
docker-compose.yml       the local stack
sports_betting/archive/          private bronze Parquet writer + catalog
sports_betting/providers/        provider adapters and normalized event snapshots
```

## CI

`.github/workflows/pr-gate.yml` runs lint, tests, and the harness drift check on
every PR. The drift check is only meaningful when it can see devkit — if it prints
"nothing to do (skipping)", the gate is inert and the wiring needs fixing.

`.github/dependabot.yml` opens weekly dependency PRs, and
`.github/workflows/dependabot-automerge.yml` merges them once the gate passes —
patch/minor bumps of anything, plus majors confined to dev tooling. A major that
touches a runtime dependency is labelled `needs-manual-merge` and waits for you.
