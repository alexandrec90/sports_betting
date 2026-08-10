"""The archive settings block — in particular that it stays a valid lake seam.

`data_lake` reads a consumer's settings through a `Protocol`, so these field names are an
interface, not local naming. A rename that looks harmless here drops this project out of
`ArchiveSettings` and the mirror stops resolving, with no import error to point at the cause.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sports_betting.config import Settings

SKIP_REASON = (
    "sibling data-lake package absent; install with `uv pip install -e ../data-lake[archive]`"
)

S3_COMPLETE = dict(
    archive_backend="s3",
    archive_s3_bucket="bronze",
    archive_s3_endpoint_url="https://acct.r2.cloudflarestorage.com",
    archive_s3_access_key_id="key",
    # noqa: ruff's hardcoded-password rule; pragma: the repo's detect-secrets gate. Both fire
    # on the literal below, which is a fixture value the validator only checks for emptiness.
    archive_s3_secret_access_key="secret",  # noqa: S106  # pragma: allowlist secret
)


def settings(**overrides):
    return Settings(_env_file=None, **overrides)


def test_the_default_backend_offloads_nothing():
    """A checkout with no mirror configured must not silently believe it has one."""
    assert settings().archive_backend == "none"


def test_r2_region_defaults_to_auto():
    """R2 rejects any other region with an opaque signature error."""
    assert settings().archive_s3_region == "auto"


def test_s3_backend_requires_its_credentials():
    with pytest.raises(ValidationError, match="ARCHIVE_S3_BUCKET"):
        settings(archive_backend="s3")


def test_s3_error_names_every_missing_field_at_once():
    with pytest.raises(ValidationError) as caught:
        settings(archive_backend="s3", archive_s3_bucket="bronze")

    message = str(caught.value)
    assert "ARCHIVE_S3_ENDPOINT_URL" in message
    assert "ARCHIVE_S3_ACCESS_KEY_ID" in message
    assert "ARCHIVE_S3_SECRET_ACCESS_KEY" in message


def test_s3_error_points_at_the_right_kind_of_cloudflare_credential():
    """A general Cloudflare API token does not authenticate against the S3 endpoint."""
    with pytest.raises(ValidationError, match="R2 > Manage API Tokens"):
        settings(archive_backend="s3")


def test_a_complete_s3_configuration_is_accepted():
    assert settings(**S3_COMPLETE).archive_s3_bucket == "bronze"


def test_local_backend_requires_a_target_directory():
    with pytest.raises(ValidationError, match="ARCHIVE_LOCAL_DIR"):
        settings(archive_backend="local")


def test_local_backend_refuses_to_mirror_the_archive_onto_itself(tmp_path):
    with pytest.raises(ValidationError, match="must differ from ARCHIVE_ROOT"):
        settings(
            archive_backend="local",
            archive_root=tmp_path,
            archive_local_dir=str(tmp_path),
        )


def test_local_backend_accepts_a_distinct_directory(tmp_path):
    resolved = settings(
        archive_backend="local",
        archive_root=tmp_path / "archive",
        archive_local_dir=str(tmp_path / "mirror"),
    )
    assert resolved.archive_local_dir.endswith("mirror")


def test_an_unknown_backend_is_rejected():
    with pytest.raises(ValidationError):
        settings(archive_backend="gdrive")


def test_settings_satisfies_the_lake_archive_protocol():
    """The seam itself. Renaming any archive_* field fails here rather than at upload time."""
    lake_settings = pytest.importorskip("data_lake.settings", reason=SKIP_REASON)

    assert isinstance(settings(**S3_COMPLETE), lake_settings.ArchiveSettings)
