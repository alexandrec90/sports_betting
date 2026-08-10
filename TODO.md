# TODO — Sports Betting

Freshly generated. These are the things the template deliberately left for you,
because guessing them wrong is worse than leaving them blank.

## Setup

- [ ] Copy `.env.example` to `.env` before overriding local paths, credentials, or ports
- [ ] Confirm `python scripts/sync-devkit.py --list` shows a stamped `DEVKIT_VERSION`
- [ ] Set `DEVKIT_DIR` in CI so the drift check actually gates — a
      `--check` that prints "nothing to do (skipping)" is checking nothing
- [ ] Replace the placeholder DB password in `.env` (the committed one is a
      local-dev placeholder and is fine to keep local, but never reuse it remotely)
- [ ] Create the **private** R2 bucket and fill the `ARCHIVE_S3_*` vars, then run
      `sports-betting archive-sync --dry-run`. Credentials are an S3-compatible key pair
      from R2 > Manage API Tokens, *not* a general Cloudflare API token. Confirm no
      `r2.dev`/custom domain is enabled — S3 credentials cannot detect a public bucket,
      only the Cloudflare API or dashboard can.
- [ ] Write `.claude/rules/data-lake.md`'s "what must never be sent" list before
      the first byte ships, not after an incident

## First real work

- [ ] Add a higher-coverage provider adapter after choosing the first target sport/league
- [ ] Add the Football-Data.co.uk historical results/odds bulk importer
- [ ] Restore bronze snapshots into an operational Postgres schema before model work

## Archive

<!-- Completed items move here. -->

- [x] Add an idempotent sports schedule/results pipeline with Parquet cataloguing
- [x] Replace the generated smoke tests with provider, archive, pipeline, and CLI tests
- [x] Schedule all four free providers with restart-safe throttling and health artifacts
- [x] Adopt the sibling lake package and build the archive mirror (upload → verify → prune)
- [x] Resolve the `_catalog/` shape collision with the lake's `DatasetManifest`
