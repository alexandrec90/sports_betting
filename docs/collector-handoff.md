# Moving the collector between PCs

The `collector` service runs on one of two Windows PCs. What it has collected lives on that
PC: the bronze datasets under `ARCHIVE_ROOT` (the pooled tree in the sibling data-lake
checkout) and the scheduler's state under `logs/`. Moving the collector moves both, through
the same S3-compatible store `archive-sync` mirrors into (`ARCHIVE_BACKEND=s3` and the
`ARCHIVE_S3_*` keys in `.env`). The Postgres `db` service is not carried: nothing reads it.

**devkit drives it.** `collectors.py move sports_betting --to HOST` stops `collector` on the
source PC, runs this repo's [`scripts/handoff.py`](../scripts/handoff.py) to export, and on
the receiving PC's next pass imports the handoff and starts `collector` there. By hand,
from the checkout on each host (not in a container):

```bash
docker compose stop collector                                    # on PC-A
uv run python scripts/handoff.py export --from pc-a --to pc-b    # on PC-A
uv run python scripts/handoff.py pending --host pc-b             # on PC-B
uv run python scripts/handoff.py import --host pc-b              # on PC-B
docker compose up -d --build collector                           # on PC-B
```

The store client comes from the sibling lake package, which is installed by hand, not
declared (see `pyproject.toml`): `uv pip install -e ../data-lake[archive]`. A plain
`uv run` keeps it -- its sync is inexact and only adds what the lock names -- but `uv sync`
removes it. Without it every command fails naming that install line.

## What travels

- **Datasets**: `sports_odds` and `sports_events`, and every dataset
  [`sports_betting/historical.py`](../sports_betting/historical.py) writes through
  `BulkArchive` (`BULK_DATASETS`), with their `_catalog/<dataset>.json` entries. Never
  `_staging`, and never another project's datasets or catalog entries, though they share
  the pooled tree.
- **State**: the four `*_file` settings in
  [`sports_betting/config.py`](../sports_betting/config.py) -- the two refresh logs, the
  provider quota ledger and the job health registry. They are what stops a new machine
  re-spending a month's paid credits or reporting every job as never run. The receiving
  machine's copies are replaced by the sender's. Run reports in `logs/` do not travel.

## The merge rule

Both PCs hold part of the history, so an import onto a machine that already has a partial
or older tree must not lose anything on either side.
[`sports_betting/archive/converge.py`](../sports_betting/archive/converge.py) holds the
rule, by kind of file:

- **Odds and events partitions** are rewritten in place by the collector, so two machines
  hold different copies of one file. The store's copy is **merged into** the local one
  through the dataset's own `merge_rows`, keyed by `(source, external_id, payload_hash)`.
  Each key is one exact observation, so the result is the union: no local row is dropped
  or changed.
- **Bulk artifacts** live in `version=<source sha>/` directories named by the publisher's
  bytes. A local file is **never overwritten**: missing files are fetched and checked
  against their `provenance.json`, present ones stay. A source pruned to the store stays
  there (`archive-restore` fetches it when needed).
- **Catalogs** are never pulled. They are rebuilt from the merged tree.

Every file lands through a temp file and a rename, so an interrupted import leaves no
half-written Parquet, and running it again carries on from where it stopped.

## What each step does

- **export** first pulls the store onto this disk under the rule above, so this tree holds
  everything. Then it pushes: `archive-sync`'s upload and verify for what the store lacks,
  plus a byte comparison for the odds and events partitions and catalogs that `archive-sync`
  never re-sends. A partition is replaced only when the store's rows are a subset of the
  local ones. Then it uploads the state files to `handoff/sports_betting/state/`, then
  `manifest.json` (every key the receiver must hold, with a sha256 for each partition and
  catalog), and last `marker.json`: `state: pending`, `from`, `to`, `at`, the manifest's
  `key`, `sha256` and `bytes`, and each state file's key and sha256. Every upload is read
  back before the next step.
- **pending** is one GET of the marker. It prints the marker when the marker is `pending`
  and addressed to this host, prints nothing otherwise, and exits 2 when the store cannot
  be read.
- **import** checks the manifest and the state files against the marker, then stops
  `collector` (the archive has one writer) and pulls. Each partition's store copy must
  match the manifest's sha256. After the pull it checks that every key the manifest lists
  is on disk, writes the state files, and rewrites the marker as `state: imported`, `by`,
  `imported_at`. It never starts `collector`; devkit does.

## When it fails

Every run overwrites `logs/handoff.json` with `command`, `ok`, `exit_code` and `detail`,
on failure too. Exit codes: 0 done, 1 failed (reason in `detail`), 2 cannot tell (only
`pending`, for an unreadable store).

- An export that fails writes no marker, so the other PC never sees a half-finished handoff.
- An import that fails leaves the marker `pending`, so the next pass retries it. Whatever
  it already pulled is complete and correct, and it writes no state file until the whole
  pull has been verified.
- `export` refuses when `ARCHIVE_BACKEND` is not `s3`, or when the lake package is missing.
