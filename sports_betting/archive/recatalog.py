"""Rewrite existing `_catalog/` manifests into the shape the shared namespace requires.

Adopting `DatasetManifest`'s key names fixed the *writers*, which is only half the collision:
manifests already on disk were written by the old code and still carry `timestamp_column`
with no per-partition `updated_at`. Those are the ones a foreign consumer actually meets, and
`DatasetManifest.from_dict` raises `ManifestError` on every one of them. A dataset that is
never re-imported would keep its unreadable manifest indefinitely, so the migration cannot be
"it fixes itself on the next write".

Every manifest is *derived* — row counts and time spans are recomputed from the Parquet — so
this is a rebuild, not an edit, and it is safe to run repeatedly.
"""

from __future__ import annotations

from pathlib import Path

from sports_betting.archive.bulk import BulkArchive
from sports_betting.archive.events import DATASET as EVENTS_DATASET
from sports_betting.archive.events import EventArchive
from sports_betting.archive.manifest import CATALOG_DIRNAME
from sports_betting.archive.odds import DATASET as ODDS_DATASET
from sports_betting.archive.odds import OddsArchive

_NON_DATASET_DIRS = frozenset({CATALOG_DIRNAME, "_staging"})


def bulk_datasets(root: Path | str) -> list[str]:
    """Every bulk dataset under `root`, identified by holding at least one provenance record."""
    base = Path(root)
    found = set()
    for provenance in base.rglob("provenance.json"):
        parts = provenance.relative_to(base).parts
        if len(parts) > 1 and parts[0] not in _NON_DATASET_DIRS:
            found.add(parts[0])
    return sorted(found)


def rebuild_catalogs(root: Path | str) -> list[str]:
    """Rewrite every manifest under `root`. Returns the dataset names rebuilt, in order."""
    base = Path(root)
    rebuilt: list[str] = []
    if (base / EVENTS_DATASET).is_dir():
        EventArchive(base).rebuild_catalog()
        rebuilt.append(EVENTS_DATASET)
    if (base / ODDS_DATASET).is_dir():
        OddsArchive(base).rebuild_catalog()
        rebuilt.append(ODDS_DATASET)
    archive = BulkArchive(base)
    for dataset in bulk_datasets(base):
        if dataset in {EVENTS_DATASET, ODDS_DATASET}:
            continue
        archive.rebuild_catalog(dataset)
        rebuilt.append(dataset)
    return rebuilt
