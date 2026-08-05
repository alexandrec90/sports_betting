# Sports Betting

Sports bet automater and data collector

## Tech Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| Database | PostgreSQL |
| Data lake (bronze) | Hive-partitioned Parquet on S3-compatible storage, read via DuckDB |
| Container | Docker + Docker Compose |
| Tests | pytest |
| Lint | ruff |

## Environment Variables

See `.env.example` for every variable. `.env` is gitignored and holds this
checkout's ports and credentials.

## Tooling

> Everything in this section needs the local Docker Desktop daemon. If it isn't
> running, make the code change and defer container/stack verification until it is
> (or to CI). Run `docker ps` first — an `npipe`/daemon error means Desktop is stopped.

### Scripts and the vendored harness

Both are covered by **`.claude/rules/engineering.md`** — script conventions (pure
importable functions, stdlib-only hooks, tests in the same change), the failure-artifact
rule, and how the `.devkit.toml` seam works. That file is vendored from
[devkit](https://github.com/alexandrec90/devkit) and drift-gated, so it is the
authority; this file does not repeat it.

### VS Code tasks

- Use `"type": "process"` so VS Code monitors the process directly — that is what
  makes the spinner stop and the exit-code icon appear reliably.
- Set `"close": false` in `presentation` so the terminal stays open for review.
- **Wrap with `notify-wrap.py`** for the completion toast; never call `notify.py`
  from inside a script. Notifications are a task-layer concern only.
- Label convention: `"Domain: Title Case Action"`, and **every task carries a
  `detail`** — that is the second line in the quick-pick, and the only place a
  one-click action can state its cost or blast radius.

### Failure artifacts (fix from a file, not from the terminal)

Any task or script whose failures an agent is expected to act on must persist the
failure to a **parseable artifact file** under `logs/`. Never rely on streamed
terminal output — it scrolls away and buries the signal. Keep the terminal to a
status line plus the artifact path; put everything needed to diagnose in the file.
Write the artifact on failure too, not just success, and overwrite per run.

### Docker subprocess calls

- **`docker compose exec` must use `-T`** — without it a pseudo-TTY is allocated and
  the subprocess handle can outlive the command, leaving the caller hung.

## Parallel worktrees

`../sports_betting-b` is a second checkout (`git worktree`) on its own branch, with its
own Docker stack. The `.git` object store, Docker image layers, and the package cache
are shared, so the second stack is cheap.

- **`COMPOSE_PROJECT_NAME` must equal the directory name** — it namespaces
  containers, network, and volumes.
- **Every `*_HOST_PORT` is offset by the checkout's slot** (this one is slot
  4, `sports_betting-b` is slot 5). Slots are assigned in
  devkit's `ports.toml`, not picked by hand; `docker compose up` failing with "port
  is already allocated" means two checkouts share a slot.
- `docker compose down -v` is project-scoped and safe to reset one stack, but
  daemon-wide commands like `docker system prune` hit both — don't run them while
  the other stack is up.

## Testing

**`.claude/rules/engineering.md`** is the authority: tests ship in the same commit,
every testable unit of logic is covered, regression test first, targeted runs locally
and full runs in CI.

Add this project's specifics *below* — fixtures, isolation rules, markers, what to mock
and where — but do not restate the policy above. It is vendored and drift-gated; a copy
here is a fork that will disagree with it the first time either is edited.

## Guardrails

Baseline guardrails — including the instruction-file feedback loop (**never silently
work around a bad instruction**) — are in `.claude/rules/engineering.md`. Rules for
writing skills and rules themselves are in `.claude/rules/authoring.md`. Cross-reference
this project's own scoped rules here, one line each.

### Data lake

See `.claude/rules/data-lake.md`. The two rules that must never be relaxed: the
bucket is **private**, and the lake is **bronze, not a source of truth** — the hot
path never reads it directly.

### Québec wagering boundary

This checkout is operated from Québec. Treat it as a **data/research application only**
unless a future task provides current legal review and an operator's written API permission.
As verified on 2026-08-04, Loto-Québec says `lotoquebec.com` is Québec's only legal online
gaming site, while Betfair's terms list Canada as a prohibited territory. Do not add Betfair
login/order code, geolocation workarounds, browser automation for wagering, or storage of
betting-account data. Re-verify law and provider terms immediately before any execution work.

### Collection scheduler

Provider jobs and quota policy belong in `sports_betting`, never the sibling lake. The lake is
a passive private storage boundary. Keep the scheduler single-writer: concurrent Parquet/catalog
writes to one `ARCHIVE_ROOT` are unsupported. Free-plan minimums are hard safety constraints:
`COLLECTION_INTERVAL_HOURS >= 6`, BALLDONTLIE requests at least 13 seconds apart, football-data
requests at least 7 seconds apart, and The Odds API claims against the persistent 20/day ledger
before every request. Do not weaken these limits without re-checking current official pricing.
Historical bulk downloads require no keys but may be large: keep backfills manual, pace requests
at least one second apart, preserve publisher files/licences/hashes, and make scheduled weekly
refresh opt-in. MoneyPuck data is non-commercial unless separate permission is obtained.
