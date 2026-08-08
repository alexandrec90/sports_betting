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
