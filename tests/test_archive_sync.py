"""Upload → verify → prune, and above all that a prune never outruns its verification."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sports_betting.archive.bulk import BulkArchive
from sports_betting.archive.sync import (
    ArchiveSync,
    restore_sources,
    scan,
    store_for,
)
from sports_betting.config import Settings


class FakeStore:
    """In-memory ObjectStore. `corrupt` rewrites a key's bytes to simulate a bad round-trip."""

    def __init__(self, *, fail_on: str | None = None):
        self.objects: dict[str, bytes] = {}
        self.fail_on = fail_on
        self.reads: list[str] = []

    def put_bytes(self, key: str, data: bytes) -> None:
        if key == self.fail_on:
            raise RuntimeError("simulated upload failure")
        self.objects[key] = data

    def get_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        try:
            return self.objects[key]
        except KeyError:
            raise KeyError(key) from None

    def exists(self, key: str) -> bool:
        return key in self.objects

    def corrupt(self, key: str) -> None:
        self.objects[key] = self.objects[key] + b"tampered"


def converter(source, target, sha256, fetched_at):
    table = pa.table({"home": ["a", "b"], "away": ["c", "d"]})
    pq.write_table(table, target)
    return table.num_rows


def build_archive(root, *, dataset="statsbomb_events", partition="match_id=1"):
    """One bulk artifact: a `source-*` file, a `data.parquet`, `provenance.json`, a catalog.

    The recorded `source_sha256` is the file's real digest, not a placeholder: `restore`
    checks a restored object against it, so a fixture that records a fake hash makes every
    restore look corrupt.
    """
    source = root / f"{dataset}-input.json"
    source.write_text('[{"id": 1}]', encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    BulkArchive(root).write(
        source,
        dataset=dataset,
        partition_parts=(partition,),
        source_url=f"https://example.test/{dataset}.json",
        source_sha256=digest,
        source_media_type="application/json",
        source_filename="events.json",
        fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
        license_name="terms",
        license_url="https://example.test/terms",
        converter=converter,
    )
    source.unlink()
    return root


def source_files(root):
    return sorted(p for p in root.rglob("source-*") if p.is_file())


def test_scan_skips_staging_and_partial_downloads(tmp_path):
    (tmp_path / "_staging" / "historical").mkdir(parents=True)
    (tmp_path / "_staging" / "historical" / "abc.part").write_bytes(b"half")
    (tmp_path / "_staging" / "historical" / "abc.json").write_bytes(b"{}")
    (tmp_path / "sports_events").mkdir()
    (tmp_path / "sports_events" / "a.parquet").write_bytes(b"data")
    (tmp_path / "sports_events" / "a.parquet.tmp").write_bytes(b"partial")

    keys = {c.key for c in scan(tmp_path)}

    assert keys == {"sports_events/a.parquet"}


def test_scan_classifies_sources_parquet_and_dataset(tmp_path):
    build_archive(tmp_path)

    by_key = {c.key: c for c in scan(tmp_path)}
    source = next(c for c in by_key.values() if c.is_source)

    assert source.dataset == "statsbomb_events"
    assert source.prunable is True
    assert next(c for c in by_key.values() if c.is_parquet).prunable is False


def test_sync_uploads_and_verifies_every_object(tmp_path):
    build_archive(tmp_path)
    store = FakeStore()

    summary = ArchiveSync(tmp_path, store, backend="local").run()

    assert summary.ok
    assert summary.uploaded == summary.scanned
    assert summary.pruned == 0
    # Nothing was deleted: an upload without --prune is a mirror, not a move.
    assert source_files(tmp_path)


def test_second_run_skips_objects_the_mirror_already_holds(tmp_path):
    build_archive(tmp_path)
    store = FakeStore()
    ArchiveSync(tmp_path, store, backend="local").run()

    summary = ArchiveSync(tmp_path, store, backend="local").run()

    assert summary.uploaded == 0
    assert summary.already_present == summary.scanned


def test_prune_deletes_only_verified_source_artifacts(tmp_path):
    build_archive(tmp_path)
    store = FakeStore()

    summary = ArchiveSync(tmp_path, store, backend="local").run(prune=True)

    assert summary.ok
    assert summary.pruned == 1
    assert summary.freed_bytes > 0
    assert source_files(tmp_path) == []
    # The queryable copy stays: BulkArchive._write_catalog re-opens it on the next import.
    assert list(tmp_path.rglob("data.parquet"))


def test_prune_leaves_the_local_copy_when_verification_fails(tmp_path):
    """The safety property. A mirror that returns different bytes must not cost the original."""
    build_archive(tmp_path)
    store = FakeStore()
    ArchiveSync(tmp_path, store, backend="local").run()
    key = next(k for k in store.objects if "source-" in k)
    store.corrupt(key)

    summary = ArchiveSync(tmp_path, store, backend="local").run(prune=True)

    assert not summary.ok
    assert summary.pruned == 0
    assert source_files(tmp_path), "a failed verification must never delete the local file"
    assert "verification failed" in summary.failures[0].detail


def test_prune_verifies_objects_uploaded_by_an_earlier_run(tmp_path):
    """An interrupted upload then a later --prune must still read back before deleting."""
    build_archive(tmp_path)
    store = FakeStore()
    ArchiveSync(tmp_path, store, backend="local").run()
    store.reads.clear()

    summary = ArchiveSync(tmp_path, store, backend="local").run(prune=True)

    assert summary.pruned == 1
    assert any("source-" in key for key in store.reads), "prune must re-read before deleting"


def test_prune_spares_datasets_whose_source_is_read_back(tmp_path):
    build_archive(tmp_path, dataset="statsbomb_matches", partition="season_id=1")

    summary = ArchiveSync(tmp_path, FakeStore(), backend="local").run(prune=True)

    assert summary.pruned == 0
    assert source_files(tmp_path), "StatsBomb re-reads this source to enumerate match ids"


def test_prune_records_the_remote_key_on_the_artifact(tmp_path):
    build_archive(tmp_path)
    store = FakeStore()

    ArchiveSync(tmp_path, store, backend="local").run(prune=True)

    provenance = json.loads(next(tmp_path.rglob("provenance.json")).read_text(encoding="utf-8"))
    assert provenance["source_pruned"] is True
    assert "source-" in provenance["source_remote_key"]


def test_prune_patches_the_catalog_so_consumers_are_not_told_the_source_is_local(tmp_path):
    """The catalog is what a foreign consumer reads; a stale row sends it to a deleted file."""
    build_archive(tmp_path)
    store = FakeStore()

    ArchiveSync(tmp_path, store, backend="local").run(prune=True)

    catalog = json.loads(
        (tmp_path / "_catalog" / "statsbomb_events.json").read_text(encoding="utf-8")
    )
    assert catalog["artifacts"][0]["source_pruned"] is True
    assert json.loads(store.objects["_catalog/statsbomb_events.json"])["artifacts"][0][
        "source_pruned"
    ]


def test_a_later_import_rebuild_preserves_the_pruned_flag(tmp_path):
    """`_write_catalog` copies provenance wholesale, so the flag must survive a rebuild."""
    build_archive(tmp_path)
    ArchiveSync(tmp_path, FakeStore(), backend="local").run(prune=True)

    build_archive(tmp_path, dataset="statsbomb_events", partition="match_id=2")

    catalog = json.loads(
        (tmp_path / "_catalog" / "statsbomb_events.json").read_text(encoding="utf-8")
    )
    pruned = [row for row in catalog["artifacts"] if row.get("source_pruned")]
    assert len(pruned) == 1


def test_pruned_provenance_is_re_uploaded_so_the_mirror_is_not_stale(tmp_path):
    build_archive(tmp_path)
    store = FakeStore()

    ArchiveSync(tmp_path, store, backend="local").run(prune=True)

    key = next(k for k in store.objects if k.endswith("provenance.json"))
    assert json.loads(store.objects[key])["source_pruned"] is True


def test_a_pruned_source_reports_how_to_get_it_back(tmp_path):
    build_archive(tmp_path)
    ArchiveSync(tmp_path, FakeStore(), backend="local").run(prune=True)
    archive = BulkArchive(tmp_path)
    digest = hashlib.sha256(b'[{"id": 1}]').hexdigest()
    artifact = archive.find_artifact("statsbomb_events", "match_id=1", digest)

    with pytest.raises(FileNotFoundError, match="archive-restore"):
        archive.source_path(artifact)


def test_restore_brings_pruned_sources_back_and_clears_the_flag(tmp_path):
    build_archive(tmp_path)
    store = FakeStore()
    ArchiveSync(tmp_path, store, backend="local").run(prune=True)
    assert source_files(tmp_path) == []

    summary = restore_sources(tmp_path, store)

    assert summary.ok
    assert summary.verified == 1
    assert source_files(tmp_path)
    provenance = json.loads(next(tmp_path.rglob("provenance.json")).read_text(encoding="utf-8"))
    assert "source_pruned" not in provenance


def test_restore_refuses_an_object_that_does_not_match_the_recorded_hash(tmp_path):
    build_archive(tmp_path)
    store = FakeStore()
    ArchiveSync(tmp_path, store, backend="local").run(prune=True)
    store.corrupt(next(k for k in store.objects if "source-" in k))

    summary = restore_sources(tmp_path, store)

    assert not summary.ok
    assert "does not match the recorded source hash" in summary.failures[0].detail
    assert source_files(tmp_path) == [], "a mismatched restore must not be written"


def test_oversize_objects_are_reported_not_attempted(tmp_path):
    build_archive(tmp_path)
    store = FakeStore()

    summary = ArchiveSync(tmp_path, store, backend="local", max_object_bytes=8).run(prune=True)

    assert not summary.ok
    assert summary.pruned == 0
    assert store.objects == {}, "an oversize object must never be read into memory"
    assert "twice its size" in summary.failures[0].detail


def test_upload_failure_is_reported_and_does_not_prune(tmp_path):
    build_archive(tmp_path)
    key = next(c.key for c in scan(tmp_path) if c.is_source)
    store = FakeStore(fail_on=key)

    summary = ArchiveSync(tmp_path, store, backend="local").run(prune=True)

    assert not summary.ok
    assert summary.pruned == 0
    assert source_files(tmp_path)


def test_dry_run_changes_nothing_but_reports_the_plan(tmp_path):
    build_archive(tmp_path)
    store = FakeStore()

    summary = ArchiveSync(tmp_path, store, backend="local").run(prune=True, dry_run=True)

    assert summary.planned == summary.scanned
    assert summary.pruned == 0
    assert store.objects == {}
    assert source_files(tmp_path)


def test_store_for_explains_an_unconfigured_backend():
    settings = Settings(archive_backend="none", _env_file=None)

    with pytest.raises(RuntimeError, match="nothing is offloaded"):
        store_for(settings)
