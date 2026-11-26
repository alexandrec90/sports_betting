"""Shared fixtures for the local end-to-end tier (`-m local`, run by `scripts/e2e.py`).

A tier test never skips: deselection already keeps it out of CI, and on the workstation it
was written for a missing prerequisite is a failure that names what to install.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from types import ModuleType

import pytest

LAKE_MISSING = (
    "the sibling data-lake package is not importable; install it with "
    "`uv pip install -e ../data-lake[archive]` (scripts/e2e.py does this when the sibling "
    "checkout is on disk)"
)


def import_lake_module(name: str) -> ModuleType:
    """Import a `data_lake` module, failing the test, never skipping it, when it is absent."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name is None or not name.startswith(exc.name):
            raise  # the package is there and something it imports is not: a real failure
        pytest.fail(f"{LAKE_MISSING} ({exc})")


@pytest.fixture
def lake_module() -> Callable[[str], ModuleType]:
    """`lake_module("data_lake.archive.catalog")`, for tests marked `local`."""
    return import_lake_module
