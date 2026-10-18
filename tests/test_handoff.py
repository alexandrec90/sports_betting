"""`scripts/handoff.py` and `sports_betting/archive/converge.py`: moving the collector's data.

No network, no Docker and no sibling lake package: the store is an in-memory fake with the
`list_objects` the real one has, and each "machine" is a `tmp_path` tree -- an archive root
shared with another project's data, plus a `logs/` of state files.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sports_betting.archive import converge
from sports_betting.archive.bulk import BulkArchive
from sports_betting.archive.events import EventArchive
from sports_betting.archive.odds import OddsArchive
from sports_betting.archive.sync import ArchiveSync
from sports_betting.providers.odds_api import OddsSnapshot
from sports_betting.providers.thesportsdb import EventSnapshot

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "handoff.py"


def load_handoff() -> ModuleType:
    """Import `scripts/handoff.py`, registered first so `@dataclass` can find its module."""
    spec = importlib.util.spec_from_file_location("handoff_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handoff = load_handoff()

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 10, 18, 6, tzinfo=UTC)
STATE_NAMES = (
    "api-sports-refresh.json",
    "odds-api-refresh.json",
    "provider-quotas.json",
    "scheduler-health.json",
)


class FakeStore:
    """The lake's `ObjectStore` over a dict, with `list_objects` and failure injection."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.fail_put: set[str] = set()
        self.fail_get: Exception | None = None

    def put_bytes(self, key: str, data: bytes) -> None:
        if key in self.fail_put:
            raise RuntimeError(f"simulated upload failure for {key}")
        self.puts.append(key)
        self.objects[key] = bytes(data)

    def get_bytes(self, key: str) -> bytes:
        if self.fail_get is not None:
            raise self.fail_get
        try:
            return self.objects[key]
        except KeyError:
            raise KeyError(key) from None

    def exists(self, key: str) -> bool:
        return key in self.objects

    def list_objects(self, prefix: str = "") -> list[SimpleNamespace]:
        return [
            SimpleNamespace(key=key, size=len(data))
            for key, data in sorted(self.objects.items())
            if key.startswith(prefix)
        ]


# --- building machines ------------------------------------------------------------------


def odds(price: float, *, event: str = "event-1") -> OddsSnapshot:
    return OddsSnapshot.from_api(
        {
            "id": event,
            "sport_title": "NBA",
            "commence_time": "2026-10-19T00:00:00Z",
            "home_team": "Home",
            "away_team": "Away",
            "bookmakers": [{"key": "example", "price": price}],
        },
        sport_key="basketball_nba",
        observed_at=OBSERVED,
    )


def event(score: str = "4") -> EventSnapshot:
    return EventSnapshot.from_thesportsdb(
        {
            "idEvent": "12345",
            "strEvent": "Home vs Away",
            "strSport": "Ice Hockey",
            "dateEvent": "2026-10-17",
            "strTimestamp": "2026-10-17T23:30:00Z",
            "strHomeTeam": "Home",
            "strAwayTeam": "Away",
            "intHomeScore": score,
            "intAwayScore": "2",
            "strStatus": "Final",
        },
        observed_at=OBSERVED,
    )


def converter(source, target, sha256, fetched_at):
    table = pa.table({"home": ["a", "b"], "away": ["c", "d"]})
    pq.write_table(table, target)
    return table.num_rows


def bulk(root: Path, *, dataset: str = "nflverse_play_by_play", body: str = "season 2025") -> str:
    """One bulk artifact under `root`; returns its version directory's key."""
    root.mkdir(parents=True, exist_ok=True)
    source = root.parent / f"{dataset}-download.csv"
    source.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    BulkArchive(root).write(
        source,
        dataset=dataset,
        partition_parts=("season=2025",),
        source_url=f"https://example.test/{dataset}.csv",
        source_sha256=digest,
        source_media_type="text/csv",
        source_filename="pbp.csv",
        fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
        license_name="terms",
        license_url="https://example.test/terms",
        converter=converter,
    )
    source.unlink()
    return f"{dataset}/season=2025/version={digest[:16]}"


def machine(tmp_path: Path, name: str):
    base = tmp_path / name
    logs = base / "checkout" / "logs"
    return handoff.Checkout(
        archive_root=base / "data-lake" / "archive",
        state_files={state: logs / state for state in STATE_NAMES},
    )


def collect(checkout, *prices: float) -> None:
    """What the collector leaves behind: odds and events partitions, a bulk import, state."""
    root = checkout.archive_root
    OddsArchive(root).write([odds(price) for price in prices or (1.9,)])
    EventArchive(root).write([event()])
    bulk(root)
    for name, path in checkout.state_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"file": name, "by": str(root)}), encoding="utf-8")


def foreign(root: Path) -> dict[str, bytes]:
    """Another project's data and a partial download in the pooled tree. Returns them."""
    files = {
        "social_scraper/posts/day=2026-10-18/posts.parquet": b"not ours",
        "_catalog/social_scraper_posts.json": b'{"dataset": "social_scraper_posts"}',
        "_staging/historical/abc.part": b"half a download",
    }
    for key, data in files.items():
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        (root / key).write_bytes(data)
    return files


def deps_for(checkout, store: FakeStore, stops: list[str] | None = None):
    return handoff.Deps(
        checkout=lambda: checkout,
        store_factory=lambda _checkout: store,
        stop_collector=lambda: (stops if stops is not None else []).append("stop"),
        now=lambda: NOW,
    )


def rows(path: Path) -> list[float]:
    """The prices held in an odds partition, sorted."""
    return sorted(
        json.loads(row["payload_json"])["bookmakers"][0]["price"]
        for row in pq.read_table(path).to_pylist()
    )


def odds_partition(root: Path) -> Path:
    (path,) = (root / "sports_odds").rglob("*.parquet")
    return path


def files_under(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def run_main(argv: list[str], deps, tmp_path: Path, capsys) -> tuple[int, dict, str]:
    artifact = tmp_path / "artifact" / "handoff.json"
    code = handoff.main(argv, deps=deps, artifact=artifact)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return code, payload, capsys.readouterr().out


@pytest.fixture
def exported(tmp_path, capsys):
    """A handed-off machine A, its store after `export --from a --to b`, and an empty B."""
    a, b = machine(tmp_path, "a"), machine(tmp_path, "b")
    collect(a, 1.9, 2.0)
    store = FakeStore()
    code, payload, _ = run_main(
        ["export", "--from", "a", "--to", "b"], deps_for(a, store), tmp_path, capsys
    )
    assert code == 0, payload
    return SimpleNamespace(a=a, b=b, store=store, payload=payload)


# --- what is carried --------------------------------------------------------------------


def test_the_scope_is_every_dataset_the_code_writes():
    tree = ast.parse((ROOT / "sports_betting" / "historical.py").read_text(encoding="utf-8"))
    literals = {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == "dataset"
        and isinstance(node.value, ast.Constant)
    }
    scope = handoff.project_scope()

    assert literals == scope.versioned
    assert set(scope.merged) == {"sports_odds", "sports_events"}
    assert not set(scope.merged) & scope.versioned


def test_the_state_files_are_the_settings_the_scheduler_reads_back():
    assert handoff.state_fields() == (
        "api_sports_refresh_file",
        "odds_refresh_file",
        "provider_quota_file",
        "scheduler_health_file",
    )


def test_settings_paths_are_read_against_the_checkout(tmp_path):
    elsewhere = tmp_path / "provider-quotas.json"
    settings = SimpleNamespace(
        archive_root=Path("../data-lake/data/archive"),
        archive_backend="s3",
        odds_refresh_file=Path("logs/odds-api-refresh.json"),
        api_sports_refresh_file=Path("logs/api-sports-refresh.json"),
        scheduler_health_file=Path("logs/scheduler-health.json"),
        provider_quota_file=elsewhere,
    )
    checkout = handoff.checkout_from_settings(settings)

    assert checkout.archive_root == handoff.REPO_ROOT / "../data-lake/data/archive"
    assert sorted(checkout.state_files) == sorted(STATE_NAMES)
    odds_file = checkout.state_files["odds-api-refresh.json"]
    assert odds_file == handoff.REPO_ROOT / "logs/odds-api-refresh.json"
    assert checkout.state_files["provider-quotas.json"] == elsewhere


def test_scope_kinds():
    scope = handoff.project_scope()

    assert (
        scope.kind("sports_odds/source=x/sport=y/event_date=2026-10-18/odds.parquet")
        == converge.MERGED
    )
    assert (
        scope.kind("nflverse_play_by_play/season=2025/version=ab/data.parquet")
        == converge.VERSIONED
    )
    assert scope.kind("_catalog/sports_odds.json") == converge.CATALOG
    assert scope.kind("_catalog/social_scraper_posts.json") is None
    assert scope.kind("social_scraper/posts.parquet") is None
    assert scope.kind("_staging/nflverse_play_by_play/x.part") is None
    assert scope.kind("sports_odds/a/odds.parquet.1234.tmp") is None
    assert scope.datasets == frozenset(scope.merged) | scope.versioned


# --- the happy path ---------------------------------------------------------------------


def test_export_then_import_moves_the_data_and_the_state(exported, tmp_path, capsys):
    a, b, store = exported.a, exported.b, exported.store
    marker = json.loads(store.objects[handoff.MARKER_KEY])
    assert marker["state"] == "pending" and (marker["from"], marker["to"]) == ("a", "b")
    assert sorted(marker["state_files"]) == sorted(STATE_NAMES)
    assert store.puts[-1] == handoff.MARKER_KEY, "the marker must be the last thing written"

    code, _, out = run_main(["pending", "--host", "B"], deps_for(b, store), tmp_path, capsys)
    assert code == 0 and json.loads(out) == marker

    stops: list[str] = []
    code, payload, out = run_main(
        ["import", "--host", "b"], deps_for(b, store, stops), tmp_path, capsys
    )
    assert code == 0, payload
    assert stops == ["stop"]
    assert out.startswith("handoff: imported from a")

    scope = handoff.project_scope()
    ours, theirs = files_under(a.archive_root), files_under(b.archive_root)
    assert set(theirs) == set(ours)
    for key, data in ours.items():
        # Catalogs are rebuilt here and merged partitions re-written by the merge, so those
        # are compared by what they hold; a bulk file is carried byte for byte.
        if scope.kind(key) == converge.VERSIONED:
            assert theirs[key] == data, key
    assert rows(odds_partition(b.archive_root)) == [1.9, 2.0]
    (events_partition,) = (b.archive_root / "sports_events").rglob("*.parquet")
    assert pq.read_table(events_partition).num_rows == 1
    for name in STATE_NAMES:
        assert b.state_files[name].read_bytes() == a.state_files[name].read_bytes()

    imported = json.loads(store.objects[handoff.MARKER_KEY])
    assert imported["state"] == "imported" and imported["by"] == "b"
    code, _, out = run_main(["pending", "--host", "b"], deps_for(b, store), tmp_path, capsys)
    assert code == 0 and out == ""


def test_the_manifest_records_what_the_receiver_must_hold(exported):
    marker = json.loads(exported.store.objects[handoff.MARKER_KEY])
    body = exported.store.objects[marker["key"]]
    assert hashlib.sha256(body).hexdigest() == marker["sha256"]
    objects = json.loads(body)["objects"]

    partition = (
        odds_partition(exported.a.archive_root).relative_to(exported.a.archive_root).as_posix()
    )
    assert objects[partition]["kind"] == "merged"
    assert (
        objects[partition]["sha256"]
        == hashlib.sha256(exported.store.objects[partition]).hexdigest()
    )
    versioned = [k for k, e in objects.items() if e["kind"] == "versioned"]
    assert versioned and all("sha256" not in objects[k] for k in versioned)
    assert marker["objects"] == len(objects)


def test_import_is_idempotent_on_a_retry(exported, tmp_path, capsys):
    deps = deps_for(exported.b, exported.store)
    assert run_main(["import", "--host", "b"], deps, tmp_path, capsys)[0] == 0
    before = files_under(exported.b.archive_root)
    marker = json.loads(exported.store.objects[handoff.MARKER_KEY])
    exported.store.objects[handoff.MARKER_KEY] = json.dumps({**marker, "state": "pending"}).encode()

    code, payload, _ = run_main(["import", "--host", "b"], deps, tmp_path, capsys)

    assert code == 0, payload
    assert payload["pulled"]["fetched"] == 0 and payload["pulled"]["merged"] == {}
    assert files_under(exported.b.archive_root) == before


# --- pending ------------------------------------------------------------------------------


def test_pending_cannot_tell_when_the_store_is_unreadable(exported, tmp_path, capsys):
    exported.store.fail_get = ConnectionError("tailnet down")

    code, payload, out = run_main(
        ["pending", "--host", "b"], deps_for(exported.b, exported.store), tmp_path, capsys
    )

    assert code == handoff.EXIT_CANNOT_TELL
    assert payload["ok"] is False and "tailnet down" in payload["detail"]
    assert out == ""


def test_pending_says_nothing_for_another_host_or_no_marker(exported, tmp_path, capsys):
    code, payload, out = run_main(
        ["pending", "--host", "c"], deps_for(exported.b, exported.store), tmp_path, capsys
    )
    assert (code, out) == (0, "") and payload["pending"] is False

    code, payload, out = run_main(
        ["pending", "--host", "b"], deps_for(exported.b, FakeStore()), tmp_path, capsys
    )
    assert (code, out) == (0, "") and payload["detail"] == "no handoff marker"


def test_pending_reason():
    assert handoff.pending_reason(None, "b") == "no handoff marker"
    assert handoff.pending_reason({"state": "pending", "to": "B"}, "b") is None
    assert "already imported" in handoff.pending_reason({"state": "imported", "by": "b"}, "b")
    assert "addressed to c" in handoff.pending_reason({"state": "pending", "to": "c"}, "b")


def test_a_marker_that_is_not_json_cannot_tell():
    store = FakeStore()
    store.objects[handoff.MARKER_KEY] = b"{half"
    with pytest.raises(handoff.CannotTell):
        handoff.read_marker(store)


# --- refusals -----------------------------------------------------------------------------


def test_import_for_the_wrong_host_fails_and_touches_nothing(exported, tmp_path, capsys):
    c = machine(tmp_path, "c")
    stops: list[str] = []

    code, payload, _ = run_main(
        ["import", "--host", "c"], deps_for(c, exported.store, stops), tmp_path, capsys
    )

    assert code == handoff.EXIT_FAILED and "addressed to b" in payload["detail"]
    assert stops == [] and not c.archive_root.exists()


def test_export_to_the_same_host_is_refused(tmp_path, capsys):
    a = machine(tmp_path, "a")
    code, payload, _ = run_main(
        ["export", "--from", "a", "--to", "A"], deps_for(a, FakeStore()), tmp_path, capsys
    )
    assert code == 1 and "same host" in payload["detail"]


def test_a_failed_upload_writes_no_marker(tmp_path, capsys):
    a = machine(tmp_path, "a")
    collect(a)
    store = FakeStore()
    store.fail_put.add(f"{handoff.STATE_PREFIX}/provider-quotas.json")

    code, payload, _ = run_main(
        ["export", "--from", "a", "--to", "b"], deps_for(a, store), tmp_path, capsys
    )

    assert code == 1 and "provider-quotas.json" in payload["detail"]
    assert handoff.MARKER_KEY not in store.objects


def test_the_store_must_be_s3():
    with pytest.raises(handoff.CannotTell, match="'s3'"):
        handoff.store_from_settings(SimpleNamespace(archive_backend="none"))


def test_a_missing_lake_package_names_the_install_command(monkeypatch):
    monkeypatch.setitem(sys.modules, "data_lake.archive.store", None)
    with pytest.raises(handoff.CannotTell) as caught:
        handoff.store_from_settings(SimpleNamespace(archive_backend="s3"))
    assert handoff.LAKE_INSTALL in str(caught.value)


def test_an_unconfigured_store_fails_export_and_cannot_tell_pending(tmp_path, capsys):
    a = machine(tmp_path, "a")
    deps = handoff.Deps(
        checkout=lambda: a,
        store_factory=lambda _c: handoff.store_from_settings(
            SimpleNamespace(archive_backend="none")
        ),
    )
    assert run_main(["export", "--from", "a", "--to", "b"], deps, tmp_path, capsys)[0] == 1
    assert run_main(["pending", "--host", "a"], deps, tmp_path, capsys)[0] == 2


def test_the_default_deps_read_nothing_until_asked():
    deps = handoff.default_deps()
    assert callable(deps.checkout) and callable(deps.store_factory)
    assert deps.stop_collector is handoff.stop_collector


def test_stop_collector_stops_the_service_and_reports_a_failure(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["cwd"]))
        return SimpleNamespace(returncode=calls and len(calls) - 1, stderr="daemon down")

    monkeypatch.setattr(handoff.subprocess, "run", fake_run)
    handoff.stop_collector()
    assert calls[0] == (["docker", "compose", "stop", "collector"], handoff.REPO_ROOT)
    with pytest.raises(handoff.HandoffError, match="daemon down"):
        handoff.stop_collector()


# --- integrity ------------------------------------------------------------------------------


def test_a_partition_that_does_not_match_the_manifest_fails_the_import(exported, tmp_path, capsys):
    b, store = exported.b, exported.store
    OddsArchive(b.archive_root).write([odds(3.0)])
    before = odds_partition(b.archive_root).read_bytes()
    key = odds_partition(exported.a.archive_root).relative_to(exported.a.archive_root).as_posix()
    other = io.BytesIO()
    pq.write_table(pq.read_table(io.BytesIO(store.objects[key])).slice(0, 1), other)
    store.objects[key] = other.getvalue()

    code, payload, _ = run_main(["import", "--host", "b"], deps_for(b, store), tmp_path, capsys)

    assert code == 1 and "the export recorded" in payload["detail"]
    assert odds_partition(b.archive_root).read_bytes() == before
    assert json.loads(store.objects[handoff.MARKER_KEY])["state"] == "pending"
    assert not any(path.is_file() for path in b.state_files.values())


@pytest.mark.parametrize("what", ["manifest", "state"])
def test_a_tampered_manifest_or_state_file_fails_before_anything_is_written(
    exported, tmp_path, capsys, what
):
    store = exported.store
    key = (
        handoff.MANIFEST_KEY
        if what == "manifest"
        else f"{handoff.STATE_PREFIX}/odds-api-refresh.json"
    )
    store.objects[key] += b" "
    stops: list[str] = []

    code, payload, _ = run_main(
        ["import", "--host", "b"], deps_for(exported.b, store, stops), tmp_path, capsys
    )

    assert code == 1 and "sha256 does not match" in payload["detail"]
    assert stops == [] and not exported.b.archive_root.exists()


def test_a_fetched_source_is_checked_against_its_provenance(exported, tmp_path, capsys):
    store = exported.store
    (source,) = [k for k in store.objects if "/source-" in k]
    store.objects[source] = b"another season entirely"

    code, payload, _ = run_main(
        ["import", "--host", "b"], deps_for(exported.b, store), tmp_path, capsys
    )

    assert code == 1 and "does not match its provenance" in payload["detail"]
    assert not (exported.b.archive_root / source).exists()


def test_check_versioned_rejects_a_parquet_with_the_wrong_row_count(tmp_path):
    target = tmp_path / "version=ab" / "data.parquet"
    target.parent.mkdir()
    (target.parent / "provenance.json").write_text(json.dumps({"rows": 5}), encoding="utf-8")
    two = io.BytesIO()
    pq.write_table(pa.table({"x": [1, 2]}), two)

    with pytest.raises(converge.ConvergeError, match="2 rows"):
        converge.check_versioned(target, two.getvalue())
    with pytest.raises(converge.ConvergeError, match="not a JSON object"):
        converge.check_versioned(target.parent / "provenance.json", b"[1]")


def test_an_interrupted_import_leaves_no_partial_file_and_resumes(
    exported, tmp_path, capsys, monkeypatch
):
    real = os.replace

    def dies_on_the_parquet(src, dst):
        if Path(dst).name == "data.parquet":
            raise OSError("power cut")
        return real(src, dst)

    monkeypatch.setattr(converge.os, "replace", dies_on_the_parquet)
    deps = deps_for(exported.b, exported.store)
    code, payload, _ = run_main(["import", "--host", "b"], deps, tmp_path, capsys)

    root = exported.b.archive_root
    assert code == 1 and "power cut" in payload["detail"]
    assert not list(root.rglob("data.parquet"))
    assert not list(root.rglob("*.tmp"))
    assert json.loads(exported.store.objects[handoff.MARKER_KEY])["state"] == "pending"

    monkeypatch.setattr(converge.os, "replace", real)
    code, payload, _ = run_main(["import", "--host", "b"], deps, tmp_path, capsys)
    assert code == 0, payload
    assert list(root.rglob("data.parquet"))


def test_write_atomic_replaces_whole_or_not_at_all(tmp_path, monkeypatch):
    target = tmp_path / "deep" / "file.bin"
    converge.write_atomic(target, b"first")
    assert target.read_bytes() == b"first"

    monkeypatch.setattr(converge.os, "replace", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        converge.write_atomic(target, b"second")
    assert target.read_bytes() == b"first"
    assert [p.name for p in target.parent.iterdir()] == ["file.bin"]


# --- the merge rule: nothing is clobbered -------------------------------------------------


def test_import_merges_into_a_newer_local_partition_and_never_replaces_it(
    exported, tmp_path, capsys
):
    b = exported.b
    OddsArchive(b.archive_root).write([odds(2.0), odds(5.5)])  # one shared, one only here

    code, payload, _ = run_main(
        ["import", "--host", "b"], deps_for(b, exported.store), tmp_path, capsys
    )

    assert code == 0, payload
    assert rows(odds_partition(b.archive_root)) == [1.9, 2.0, 5.5]
    catalog = json.loads((b.archive_root / "_catalog" / "sports_odds.json").read_text())
    assert [entry["rows"] for entry in catalog["partitions"].values()] == [3]


def test_export_merges_the_stores_rows_in_before_it_uploads(tmp_path, capsys):
    a, b = machine(tmp_path, "a"), machine(tmp_path, "b")
    store = FakeStore()
    OddsArchive(b.archive_root).write([odds(1.1)])
    ArchiveSync(b.archive_root, store).run()  # B mirrored its own history earlier
    collect(a, 1.9)

    code, payload, _ = run_main(
        ["export", "--from", "a", "--to", "b"], deps_for(a, store), tmp_path, capsys
    )

    assert code == 0, payload
    key = odds_partition(a.archive_root).relative_to(a.archive_root).as_posix()
    assert rows(odds_partition(a.archive_root)) == [1.1, 1.9]
    assert store.objects[key] == odds_partition(a.archive_root).read_bytes()


def test_push_refuses_to_drop_rows_only_the_store_holds(tmp_path):
    store, scope = FakeStore(), handoff.project_scope()
    remote = tmp_path / "remote"
    OddsArchive(remote).write([odds(1.1)])
    key = odds_partition(remote).relative_to(remote).as_posix()
    store.objects[key] = odds_partition(remote).read_bytes()
    local = tmp_path / "local"
    OddsArchive(local).write([odds(1.9)])

    with pytest.raises(converge.ConvergeError, match="1 row"):
        converge.push(local, store, scope, backend="s3")
    assert store.objects[key] == odds_partition(remote).read_bytes()


def test_a_local_bulk_file_is_never_overwritten(exported, tmp_path, capsys):
    b = exported.b
    version = bulk(b.archive_root)
    data = b.archive_root / version / "data.parquet"
    pq.write_table(pa.table({"home": ["a", "b"], "away": ["c", "d"]}), data, compression="gzip")
    mine = data.read_bytes()
    assert mine != exported.store.objects[f"{version}/data.parquet"]

    code, payload, _ = run_main(
        ["import", "--host", "b"], deps_for(b, exported.store), tmp_path, capsys
    )

    assert code == 0, payload
    assert data.read_bytes() == mine


def test_a_pruned_source_stays_in_the_store(tmp_path, capsys):
    a, b = machine(tmp_path, "a"), machine(tmp_path, "b")
    collect(a)
    store = FakeStore()
    ArchiveSync(a.archive_root, store).run(prune=True)
    (source,) = [k for k in store.objects if "/source-" in k]
    assert not (a.archive_root / source).exists()

    assert (
        run_main(["export", "--from", "a", "--to", "b"], deps_for(a, store), tmp_path, capsys)[0]
        == 0
    )
    assert not (a.archive_root / source).exists(), "the export's pull must not undo a prune"
    manifest = json.loads(store.objects[handoff.MANIFEST_KEY])
    assert manifest["objects"][source] == {"kind": "versioned", "remote_only": True}

    code, payload, _ = run_main(["import", "--host", "b"], deps_for(b, store), tmp_path, capsys)
    assert code == 0, payload
    assert not (b.archive_root / source).exists()
    provenance = json.loads(
        (b.archive_root / source).parent.joinpath("provenance.json").read_text()
    )
    assert provenance["source_pruned"] is True


def test_other_projects_and_staging_are_never_carried(tmp_path, capsys):
    a, b = machine(tmp_path, "a"), machine(tmp_path, "b")
    collect(a)
    local_foreign = foreign(a.archive_root)
    store = FakeStore()
    store_foreign = {
        "social_scraper/posts/day=2026-10-01/posts.parquet": b"theirs, in the store",
        "_catalog/social_scraper_posts.json": b'{"in": "store"}',
        "_staging/historical/zzz.part": b"remote partial",
    }
    store.objects.update(store_foreign)
    b_foreign = foreign(b.archive_root)

    assert (
        run_main(["export", "--from", "a", "--to", "b"], deps_for(a, store), tmp_path, capsys)[0]
        == 0
    )
    assert run_main(["import", "--host", "b"], deps_for(b, store), tmp_path, capsys)[0] == 0

    for key, data in store_foreign.items():
        assert store.objects[key] == data, key
    assert not set(local_foreign) - set(store_foreign) & set(store.objects)
    assert files_under(a.archive_root).items() >= local_foreign.items()
    held = files_under(b.archive_root)
    assert held.items() >= b_foreign.items()
    assert "social_scraper/posts/day=2026-10-01/posts.parquet" not in held
    assert "_staging/historical/zzz.part" not in held


# --- the converge primitives ---------------------------------------------------------------


def test_listing_puts_each_provenance_before_the_files_it_checks():
    store = FakeStore()
    for name in ("data.parquet", "provenance.json", "source-x.csv"):
        store.objects[f"nflverse_play_by_play/season=1/version=a/{name}"] = b"x"
    store.objects["social_scraper/x"] = b"x"

    keys = [o.key.rsplit("/", 1)[1] for o in converge.listing(store, handoff.project_scope())]

    assert keys == ["provenance.json", "data.parquet", "source-x.csv"]


def test_pull_with_a_narrow_scope_touches_only_that_scope(tmp_path):
    key_columns = ("source", "external_id", "payload_hash")
    scope = converge.Scope(
        merged={"sports_odds": converge.MergedDataset(OddsArchive, key_columns)},
        versioned=frozenset(),
    )
    remote = tmp_path / "remote"
    OddsArchive(remote).write([odds(1.1)])
    bulk(remote)
    store: converge.ListingStore = FakeStore()
    for key, data in files_under(remote).items():
        store.put_bytes(key, data)
    merger: converge.Merger = OddsArchive(tmp_path / "local")
    listed: list[converge.Listed] = converge.listing(store, scope)

    report = converge.pull(tmp_path / "local", store, scope)

    assert [entry.key.split("/")[0] for entry in listed] == ["sports_odds"]
    assert list(report.merged.values()) == [1] and report.catalogs == ["sports_odds"]
    assert not (tmp_path / "local" / "nflverse_play_by_play").exists()
    assert merger.merge_rows(Path(listed[0].key), []) == 0


def test_rebuild_catalogs_skips_datasets_with_no_data_here(tmp_path):
    scope, root = handoff.project_scope(), tmp_path / "archive"
    bulk(root)
    rebuilt = converge.rebuild_catalogs(root, scope, ["nflverse_play_by_play", "sports_odds"])
    assert rebuilt == ["nflverse_play_by_play"]


def test_verify_names_what_did_not_arrive(tmp_path):
    report = converge.PullReport()
    (tmp_path / "here.parquet").write_bytes(b"x")
    objects = {
        "gone/data.parquet": {"kind": "versioned"},
        "pruned/source-x": {"kind": "versioned", "remote_only": True},
        "here.parquet": {"kind": "merged", "sha256": "ab"},
    }

    problems = converge.verify(tmp_path, objects, report)

    assert problems == [
        "gone/data.parquet: not on this disk after the pull",
        "here.parquet: the store does not hold the partition the export recorded",
    ]


def test_inventory_and_report_shapes(exported):
    objects = converge.inventory(exported.a.archive_root, handoff.project_scope())
    assert {e["kind"] for e in objects.values()} == {"merged", "versioned", "catalog"}
    assert converge.sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()
    assert set(converge.PullReport().as_dict()) >= {"fetched", "merged", "kept", "remote_only"}


def test_helpers_round_trip_through_the_store():
    store = FakeStore()
    digest = handoff.put_verified(store, "k", b"body", "thing")
    assert handoff.fetch_verified(store, "k", digest, "thing") == b"body"
    with pytest.raises(handoff.HandoffError, match="sha256 does not match"):
        handoff.fetch_verified(store, "k", "0" * 64, "thing")
    handoff.write_marker(store, {"state": "pending"})
    assert handoff.read_marker(store) == {"state": "pending"}
    assert handoff.iso_utc(NOW) == "2026-10-18T12:00:00Z"
    assert handoff.parse_args(["pending", "--host", "x"]).host == "x"


def test_state_files_the_checkout_does_not_know_are_refused(tmp_path):
    a = machine(tmp_path, "a")
    marker = {"state_files": {"../../evil.json": {"key": "k", "sha256": "x"}}}
    with pytest.raises(handoff.HandoffError, match="does not know"):
        handoff.fetch_state(FakeStore(), marker, a)


def test_upload_state_skips_files_this_machine_does_not_have(tmp_path):
    a = machine(tmp_path, "a")
    path = a.state_files["provider-quotas.json"]
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    store = FakeStore()

    state = handoff.upload_state(store, a)

    assert list(state) == ["provider-quotas.json"]
    assert store.objects[state["provider-quotas.json"]["key"]] == b"{}"


def test_load_manifest_and_import_and_export_entry_points(exported):
    marker = json.loads(exported.store.objects[handoff.MARKER_KEY])
    objects = handoff.load_manifest(exported.store, marker)
    assert objects and all("kind" in e for e in objects.values())
    result = handoff.import_handoff(deps_for(exported.b, exported.store), "b")
    assert result["marker"]["state"] == "imported"
    again = handoff.export(deps_for(exported.b, exported.store), "b", "a")
    assert again["marker"]["to"] == "a" and again["pulled"]["fetched"] == 0


def test_run_reports_a_failure_as_exit_one(tmp_path):
    a = machine(tmp_path, "a")
    args = handoff.parse_args(["import", "--host", "a"])
    code, payload, line = handoff.run(args, deps_for(a, FakeStore()))
    assert code == 1 and payload["ok"] is False and line.startswith("handoff import FAILED")


def test_write_artifact_creates_its_directory(tmp_path):
    path = tmp_path / "logs" / "handoff.json"
    handoff.write_artifact(path, {"ok": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}


def test_the_marker_and_manifest_live_under_the_projects_handoff_prefix():
    assert handoff.MARKER_KEY == "handoff/sports_betting/marker.json"
    assert handoff.MANIFEST_KEY.startswith("handoff/sports_betting/")
    assert not handoff.project_scope().kind(handoff.MARKER_KEY)
