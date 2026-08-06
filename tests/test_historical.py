import hashlib
import io
import json
import zipfile

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from sports_betting.historical import HistoricalImporters, ResumableDownloader, season_code


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_football_data_import_is_versioned_cataloged_and_conditionally_idempotent(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            content=b"Div,Date,HomeTeam,AwayTeam,FTHG\nE0,01/08/26,Arsenal,Chelsea,2\n",
            headers={"content-type": "text/csv", "etag": '"v1"'},
        )

    with HistoricalImporters(tmp_path, client=client(handler)) as importers:
        first = importers.football_data(start_year=2026, end_year=2026, leagues=["E0"])
        repeat = importers.football_data(start_year=2026, end_year=2026, leagues=["E0"])

    assert first.added == 1
    assert first.rows == 1
    assert repeat.added == 0
    assert requests[1].headers["if-none-match"] == '"v1"'
    catalog = json.loads((tmp_path / "_catalog" / "football_data_uk_matches.json").read_text())
    artifact = catalog["artifacts"][0]
    assert artifact["license_url"] == "https://www.football-data.co.uk/data.php"
    assert artifact["partition"] == "league=E0/season=2627"
    assert (tmp_path / artifact["source_file"]).read_bytes().startswith(b"Div,Date")
    rows = pq.read_table(tmp_path / artifact["data_file"]).to_pylist()
    assert rows[0]["HomeTeam"] == "Arsenal"
    assert rows[0]["source_row_number"] == 0
    assert rows[0]["source_sha256"] == artifact["source_sha256"]


def test_nflverse_parquet_and_moneypuck_zip_become_training_parquet(tmp_path):
    parquet = io.BytesIO()
    pq.write_table(pa.table({"game_id": ["2025_01_A_B"], "yards_gained": [7.0]}), parquet)
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("shots_2025.csv", "shotID,teamCode,xGoal\n1,MTL,0.25\n")

    def handler(request):
        if request.url.path.endswith(".parquet"):
            return httpx.Response(200, content=parquet.getvalue())
        return httpx.Response(
            200, content=zipped.getvalue(), headers={"content-type": "application/zip"}
        )

    with HistoricalImporters(tmp_path, client=client(handler)) as importers:
        nfl = importers.nflverse_pbp(start_year=2025, end_year=2025)
        nhl = importers.moneypuck_shots(start_year=2025, end_year=2025)

    assert nfl.rows == 1
    assert nhl.rows == 1
    nfl_catalog = json.loads((tmp_path / "_catalog" / "nflverse_play_by_play.json").read_text())
    nhl_catalog = json.loads((tmp_path / "_catalog" / "moneypuck_shots.json").read_text())
    assert pq.read_table(tmp_path / nfl_catalog["artifacts"][0]["data_file"]).num_rows == 1
    assert (
        pq.read_table(tmp_path / nhl_catalog["artifacts"][0]["data_file"]).to_pylist()[0][
            "teamCode"
        ]
        == "MTL"
    )


def test_statsbomb_imports_match_index_and_each_event_file(tmp_path):
    def handler(request):
        if "/matches/9/281.json" in request.url.path:
            payload = [{"match_id": 1001, "home_team": {"home_team_name": "A"}}]
        elif "/events/1001.json" in request.url.path:
            payload = [{"id": "event-1", "type": {"name": "Pass"}}]
        else:
            raise AssertionError(str(request.url))
        return httpx.Response(200, json=payload)

    with HistoricalImporters(tmp_path, client=client(handler)) as importers:
        summary = importers.statsbomb(competition_id=9, season_id=281)

    assert summary.attempted == 2
    assert summary.rows == 2
    catalog = json.loads((tmp_path / "_catalog" / "statsbomb_events.json").read_text())
    row = pq.read_table(tmp_path / catalog["artifacts"][0]["data_file"]).to_pylist()[0]
    assert row["external_id"] == "event-1"
    assert json.loads(row["payload_json"])["type"]["name"] == "Pass"


def test_downloader_resumes_partial_file(tmp_path):
    url = "https://example.test/history.csv"
    key = hashlib.sha256(url.encode()).hexdigest()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / f"{key}.part").write_bytes(b"abc")

    def handler(request):
        assert request.headers["range"] == "bytes=3-"
        return httpx.Response(206, content=b"def", headers={"content-type": "text/csv"})

    with ResumableDownloader(staging, client=client(handler)) as downloader:
        result = downloader.download(url)

    assert result.path.read_bytes() == b"abcdef"
    assert result.sha256 == hashlib.sha256(b"abcdef").hexdigest()


def test_season_codes_wrap_century_and_year_ranges_are_validated():
    assert season_code(1999) == "9900"
    assert season_code(2026) == "2627"
