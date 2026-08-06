"""Free historical dataset downloaders and bulk importers."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from sports_betting.archive import BulkArchive, BulkWriteResult

FOOTBALL_DATA_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
NFLVERSE_PBP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"
)
STATSBOMB_RAW_URL = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
MONEYPUCK_SHOTS_URL = "https://peter-tanner.com/moneypuck/downloads/shots_{season}.zip"
MONEYPUCK_GAMES_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/all_teams.csv"

FOOTBALL_DATA_LICENSE = ("Football-Data.co.uk terms", "https://www.football-data.co.uk/data.php")
NFLVERSE_LICENSE = ("CC BY 4.0", "https://github.com/nflverse/nflverse-data")
STATSBOMB_LICENSE = (
    "StatsBomb Open Data attribution terms",
    "https://github.com/statsbomb/open-data",
)
MONEYPUCK_LICENSE = ("Non-commercial use with attribution", "https://moneypuck.com/data.htm")


@dataclass(frozen=True)
class DownloadResult:
    url: str
    path: Path | None
    sha256: str
    media_type: str
    filename: str
    fetched_at: datetime
    not_modified: bool = False


@dataclass(frozen=True)
class BulkImportSummary:
    source: str
    attempted: int
    added: int
    rows: int
    artifacts: tuple[BulkWriteResult, ...]


def season_code(start_year: int) -> str:
    if not 1990 <= start_year <= 2098:
        raise ValueError("football season start year must be between 1990 and 2098")
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def inclusive_years(start: int, end: int) -> range:
    if start > end:
        raise ValueError("from-season must not be after to-season")
    return range(start, end + 1)


class ResumableDownloader:
    """Stream downloads with range resume, validators, and a hard size ceiling."""

    def __init__(
        self,
        staging_root: Path | str,
        *,
        client: httpx.Client | None = None,
        before_request: Callable[[], None] | None = None,
        max_bytes: int = 2_000_000_000,
    ):
        self.staging_root = Path(staging_root)
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(120, connect=20),
            follow_redirects=True,
            headers={"User-Agent": "sports-betting-bulk-importer/0.1"},
        )
        self._owns_client = client is None
        self.before_request = before_request
        self.max_bytes = max_bytes

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> ResumableDownloader:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def download(self, url: str) -> DownloadResult:
        key = hashlib.sha256(url.encode()).hexdigest()
        self.staging_root.mkdir(parents=True, exist_ok=True)
        partial = self.staging_root / f"{key}.part"
        metadata_path = self.staging_root / f"{key}.json"
        metadata = self._read_metadata(metadata_path)
        headers: dict[str, str] = {}
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset:
            headers["Range"] = f"bytes={offset}-"
        elif metadata:
            if metadata.get("etag"):
                headers["If-None-Match"] = str(metadata["etag"])
            if metadata.get("last_modified"):
                headers["If-Modified-Since"] = str(metadata["last_modified"])

        if self.before_request:
            self.before_request()
        fetched_at = datetime.now(UTC)
        with self.client.stream("GET", url, headers=headers) as response:
            if response.status_code == 304 and metadata:
                return DownloadResult(
                    url=url,
                    path=None,
                    sha256=str(metadata["sha256"]),
                    media_type=str(metadata["media_type"]),
                    filename=str(metadata["filename"]),
                    fetched_at=fetched_at,
                    not_modified=True,
                )
            response.raise_for_status()
            append = response.status_code == 206 and offset > 0
            total = offset if append else 0
            content_length = int(response.headers.get("content-length", 0))
            if total + content_length > self.max_bytes:
                raise ValueError(f"download exceeds {self.max_bytes} byte safety limit: {url}")
            mode = "ab" if append else "wb"
            with partial.open(mode) as handle:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ValueError(
                            f"download exceeds {self.max_bytes} byte safety limit: {url}"
                        )
                    handle.write(chunk)

            media_type = response.headers.get("content-type", "application/octet-stream").split(
                ";", 1
            )[0]
            if media_type == "text/html":
                raise ValueError(f"expected a data file but received HTML: {url}")
            filename = Path(urlparse(str(response.url)).path).name or "download"
            digest = _sha256_file(partial)
            self._write_metadata(
                metadata_path,
                {
                    "url": url,
                    "sha256": digest,
                    "media_type": media_type,
                    "filename": filename,
                    "etag": response.headers.get("etag", ""),
                    "last_modified": response.headers.get("last-modified", ""),
                },
            )
            return DownloadResult(
                url=url,
                path=partial,
                sha256=digest,
                media_type=media_type,
                filename=filename,
                fetched_at=fetched_at,
            )

    @staticmethod
    def _read_metadata(path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write_metadata(path: Path, payload: dict) -> None:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


class HistoricalImporters:
    """Source-specific historical imports sharing one throttled downloader."""

    def __init__(
        self,
        archive_root: Path | str,
        *,
        client: httpx.Client | None = None,
        before_request: Callable[[], None] | None = None,
        max_download_bytes: int = 2_000_000_000,
    ):
        self.root = Path(archive_root)
        self.archive = BulkArchive(self.root)
        self.downloader = ResumableDownloader(
            self.root / "_staging" / "historical",
            client=client,
            before_request=before_request,
            max_bytes=max_download_bytes,
        )

    def close(self) -> None:
        self.downloader.close()

    def __enter__(self) -> HistoricalImporters:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def football_data(
        self, *, start_year: int, end_year: int, leagues: Iterable[str]
    ) -> BulkImportSummary:
        results = []
        clean_leagues = tuple(dict.fromkeys(league.strip().upper() for league in leagues if league))
        if not clean_leagues:
            raise ValueError("at least one Football-Data league code is required")
        for year in inclusive_years(start_year, end_year):
            code = season_code(year)
            for league in clean_leagues:
                url = FOOTBALL_DATA_URL.format(season=code, league=league)
                results.append(
                    self._import(
                        url,
                        dataset="football_data_uk_matches",
                        partition_parts=(f"league={league}", f"season={code}"),
                        license_terms=FOOTBALL_DATA_LICENSE,
                        converter=_csv_converter,
                    )[0]
                )
        return _summary("football-data", results)

    def nflverse_pbp(self, *, start_year: int, end_year: int) -> BulkImportSummary:
        results = []
        for year in inclusive_years(start_year, end_year):
            url = NFLVERSE_PBP_URL.format(season=year)
            results.append(
                self._import(
                    url,
                    dataset="nflverse_play_by_play",
                    partition_parts=(f"season={year}",),
                    license_terms=NFLVERSE_LICENSE,
                    converter=_parquet_converter,
                )[0]
            )
        return _summary("nflverse", results)

    def moneypuck_shots(self, *, start_year: int, end_year: int) -> BulkImportSummary:
        results = []
        for year in inclusive_years(start_year, end_year):
            url = MONEYPUCK_SHOTS_URL.format(season=year)
            results.append(
                self._import(
                    url,
                    dataset="moneypuck_shots",
                    partition_parts=(f"season={year}",),
                    license_terms=MONEYPUCK_LICENSE,
                    converter=_zip_csv_converter,
                )[0]
            )
        return _summary("moneypuck", results)

    def moneypuck_games(self) -> BulkImportSummary:
        result, _ = self._import(
            MONEYPUCK_GAMES_URL,
            dataset="moneypuck_team_games",
            partition_parts=("scope=all-seasons",),
            license_terms=MONEYPUCK_LICENSE,
            converter=_csv_converter,
        )
        return _summary("moneypuck-games", [result])

    def statsbomb(
        self, *, competition_id: int, season_id: int, max_matches: int | None = None
    ) -> BulkImportSummary:
        matches_url = f"{STATSBOMB_RAW_URL}/matches/{competition_id}/{season_id}.json"
        matches_result, matches_path = self._import(
            matches_url,
            dataset="statsbomb_matches",
            partition_parts=(
                f"competition_id={competition_id}",
                f"season_id={season_id}",
            ),
            license_terms=STATSBOMB_LICENSE,
            converter=_json_converter("match_id"),
        )
        matches = json.loads(matches_path.read_text(encoding="utf-8-sig"))
        if not isinstance(matches, list):
            raise ValueError("StatsBomb matches payload is not a list")
        selected = matches[:max_matches] if max_matches is not None else matches
        results = [matches_result]
        for match in selected:
            match_id = int(match["match_id"])
            result, _ = self._import(
                f"{STATSBOMB_RAW_URL}/events/{match_id}.json",
                dataset="statsbomb_events",
                partition_parts=(
                    f"competition_id={competition_id}",
                    f"season_id={season_id}",
                    f"match_id={match_id}",
                ),
                license_terms=STATSBOMB_LICENSE,
                converter=_json_converter("id"),
            )
            results.append(result)
        return _summary("statsbomb", results)

    def statsbomb_competitions(self) -> list[dict]:
        downloaded = self.downloader.download(f"{STATSBOMB_RAW_URL}/competitions.json")
        if downloaded.path is None:
            metadata = self.archive.find_artifact(
                "statsbomb_competitions", "scope=all", downloaded.sha256
            )
            if metadata is None:
                raise RuntimeError("download returned not-modified without an archived artifact")
            path = self.archive.source_path(metadata)
        else:
            result, path = self._archive_download(
                downloaded,
                dataset="statsbomb_competitions",
                partition_parts=("scope=all",),
                license_terms=STATSBOMB_LICENSE,
                converter=_json_converter("competition_id"),
            )
            del result
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list):
            raise ValueError("StatsBomb competitions payload is not a list")
        return payload

    def _import(
        self,
        url: str,
        *,
        dataset: str,
        partition_parts: tuple[str, ...],
        license_terms: tuple[str, str],
        converter,
    ) -> tuple[BulkWriteResult, Path]:
        downloaded = self.downloader.download(url)
        return self._archive_download(
            downloaded,
            dataset=dataset,
            partition_parts=partition_parts,
            license_terms=license_terms,
            converter=converter,
        )

    def _archive_download(
        self,
        downloaded: DownloadResult,
        *,
        dataset: str,
        partition_parts: tuple[str, ...],
        license_terms: tuple[str, str],
        converter,
    ) -> tuple[BulkWriteResult, Path]:
        logical_partition = "/".join(partition_parts)
        existing = self.archive.find_artifact(dataset, logical_partition, downloaded.sha256)
        if downloaded.path is None:
            if existing is None:
                raise RuntimeError("download returned not-modified without an archived artifact")
            return (
                BulkWriteResult(
                    dataset,
                    logical_partition,
                    downloaded.url,
                    downloaded.sha256,
                    int(existing["rows"]),
                    False,
                ),
                self.archive.source_path(existing),
            )

        result = self.archive.write(
            downloaded.path,
            dataset=dataset,
            partition_parts=partition_parts,
            source_url=downloaded.url,
            source_sha256=downloaded.sha256,
            source_media_type=downloaded.media_type,
            source_filename=downloaded.filename,
            fetched_at=downloaded.fetched_at,
            license_name=license_terms[0],
            license_url=license_terms[1],
            converter=converter,
        )
        artifact = self.archive.find_artifact(dataset, logical_partition, downloaded.sha256)
        if artifact is None:
            raise RuntimeError("bulk artifact missing after verified write")
        downloaded.path.unlink(missing_ok=True)
        return result, self.archive.source_path(artifact)


def _summary(source: str, results: list[BulkWriteResult]) -> BulkImportSummary:
    return BulkImportSummary(
        source=source,
        attempted=len(results),
        added=sum(result.added for result in results),
        rows=sum(result.rows for result in results if result.added),
        artifacts=tuple(results),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance_arrays(batch_rows: int, sha256: str, fetched_at: datetime, offset: int):
    return (
        pa.array([sha256] * batch_rows, type=pa.string()),
        pa.array(range(offset, offset + batch_rows), type=pa.int64()),
        pa.array([fetched_at] * batch_rows, type=pa.timestamp("us", tz="UTC")),
    )


def _append_provenance(table: pa.Table, sha256: str, fetched_at: datetime, offset: int) -> pa.Table:
    digest, row_numbers, timestamps = _provenance_arrays(table.num_rows, sha256, fetched_at, offset)
    return (
        table.append_column("source_sha256", digest)
        .append_column("source_row_number", row_numbers)
        .append_column("source_fetched_at", timestamps)
    )


def _write_tables(
    tables: Iterable[pa.Table], target: Path, sha256: str, fetched_at: datetime
) -> int:
    writer = None
    rows = 0
    try:
        for table in tables:
            enriched = _append_provenance(table, sha256, fetched_at, rows)
            if writer is None:
                writer = pq.ParquetWriter(target, enriched.schema, compression="zstd")
            writer.write_table(enriched)
            rows += enriched.num_rows
        if writer is None:
            empty = pa.table(
                {
                    "source_sha256": pa.array([], type=pa.string()),
                    "source_row_number": pa.array([], type=pa.int64()),
                    "source_fetched_at": pa.array([], type=pa.timestamp("us", tz="UTC")),
                }
            )
            writer = pq.ParquetWriter(target, empty.schema, compression="zstd")
            writer.write_table(empty)
    finally:
        if writer is not None:
            writer.close()
    return rows


def _csv_tables(path: Path) -> Iterable[pa.Table]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        headers = next(csv.reader(handle), [])
    if not headers:
        return
    column_types = {header: pa.string() for header in headers}
    reader = pacsv.open_csv(
        path,
        read_options=pacsv.ReadOptions(encoding="utf8", skip_rows=1, column_names=headers),
        convert_options=pacsv.ConvertOptions(column_types=column_types, strings_can_be_null=True),
    )
    for batch in reader:
        yield pa.Table.from_batches([batch])


def _csv_converter(path: Path, target: Path, sha256: str, fetched_at: datetime) -> int:
    return _write_tables(_csv_tables(path), target, sha256, fetched_at)


def _zip_csv_converter(path: Path, target: Path, sha256: str, fetched_at: datetime) -> int:
    extracted = target.with_suffix(".csv.tmp")
    try:
        with zipfile.ZipFile(path) as archive:
            members = [
                member
                for member in archive.infolist()
                if not member.is_dir() and member.filename.lower().endswith(".csv")
            ]
            if len(members) != 1:
                raise ValueError(f"expected one CSV in ZIP, found {len(members)}")
            member = members[0]
            if member.file_size > 8_000_000_000:
                raise ValueError("uncompressed MoneyPuck CSV exceeds 8 GB safety limit")
            with archive.open(member) as source, extracted.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
        return _csv_converter(extracted, target, sha256, fetched_at)
    finally:
        extracted.unlink(missing_ok=True)


def _parquet_converter(path: Path, target: Path, sha256: str, fetched_at: datetime) -> int:
    source = pq.ParquetFile(path)
    tables = (pa.Table.from_batches([batch]) for batch in source.iter_batches(batch_size=65536))
    return _write_tables(tables, target, sha256, fetched_at)


def _json_converter(id_column: str):
    def convert(path: Path, target: Path, sha256: str, fetched_at: datetime) -> int:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list):
            raise ValueError("expected a JSON list")
        rows = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise ValueError("expected every JSON list item to be an object")
            rows.append(
                {
                    "external_id": str(item.get(id_column, index)),
                    "payload_json": json.dumps(item, sort_keys=True, separators=(",", ":")),
                }
            )
        return _write_tables([pa.Table.from_pylist(rows)], target, sha256, fetched_at)

    return convert
