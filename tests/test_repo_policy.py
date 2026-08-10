import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_declares_no_path_dependencies():
    """A `[tool.uv.sources]` path entry breaks `uv sync` everywhere the path is absent.

    uv re-reads the metadata of every path source on each sync, to decide whether the
    lock is still fresh, and it does that *before* it selects extras or groups. So a
    sibling-checkout dependency cannot be hidden behind an extra: an absent sibling
    fails the sync outright, in exactly the two environments that never want it — the
    single-repo CI checkout and the Docker build, whose context is `.`.

    This bit once: the `lake` extra pointed at `../data-lake` and reddened every job on
    the PR gate. The sibling is installed separately now — see `pyproject.toml`.
    """
    sources = _pyproject().get("tool", {}).get("uv", {}).get("sources", {})

    path_sources = {name: spec for name, spec in sources.items() if "path" in spec}
    assert not path_sources, (
        f"path sources break `uv sync` wherever the path is absent (CI, Docker): {path_sources}. "
        "Install a sibling checkout separately instead: uv pip install -e ../data-lake[archive]"
    )


def test_lake_package_is_not_a_declared_dependency():
    """The same rule, stated against the requirement lists rather than the sources table.

    Declaring `data-lake` without a path source is worse, not better: there is no such
    package on the index this project resolves against, so it would fail to resolve
    rather than fail to build — and a name that free is one someone else can claim.
    """
    project = _pyproject()["project"]
    declared = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)

    offenders = [req for req in declared if req.lower().startswith("data-lake")]
    assert not offenders, f"the sibling lake package must stay undeclared: {offenders}"


def test_agent_context_preserves_quebec_wagering_boundary():
    context = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "Québec wagering boundary" in context
    assert "data/research application only" in context
    assert "Betfair's terms list Canada as a prohibited territory" in context
