import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# `uses: ./path`, the local-composite-action form. It carries no `@ref`, which is what
# distinguishes it from a third-party action and also what keeps the vendored
# `test_third_party_actions_are_pinned_to_an_immutable_ref` from ever looking at it.
LOCAL_USES = re.compile(r"^\s*-?\s*uses:\s*(\./\S+)", re.M)


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


def test_every_local_composite_action_a_workflow_uses_exists():
    """A `uses: ./…` naming a directory with no action file fails the job, not the run.

    GitHub resolves a local composite action from the checked-out workspace at step
    time, so a missing one produces `Can't find 'action.yml' … Did you forget to run
    actions/checkout` — a message that points at the *caller* rather than at the file
    that went. Every job whose first real step is that action dies before it runs a
    single check, which is what the whole gate reduces to here: three of four jobs.

    That is not hypothetical. `.github/actions/setup-python-env/action.yml` was
    vendored by devkit v0.7.0 and un-vendored in v0.8.0, so `--pull` deleted it as a
    no-longer-managed file. It is a `templates/` render now, and `templates/` is a
    one-shot copy that never revisits an existing project — nothing put it back, and
    nothing on either side reported that it had gone. This test is the half neither
    tier can do: the workflows name what they need, so they are the only honest
    inventory of it.
    """
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflows found — the CI layout moved"

    missing = []
    referenced = 0
    for workflow in workflows:
        for ref in LOCAL_USES.findall(workflow.read_text(encoding="utf-8")):
            referenced += 1
            action = ROOT / ref[len("./") :]
            if not any((action / name).is_file() for name in ("action.yml", "action.yaml")):
                missing.append(f"{workflow.name}: {ref}")

    assert referenced, "no local composite action is referenced — the scan is inert"
    assert not missing, (
        f"workflows reference local composite actions that do not exist: {missing}. "
        "Every job using one dies before its first check. If a devkit pull removed the "
        "file, restore it from devkit's templates/core/dot-github/ — it is a project-"
        "owned render, not a vendored file, so `--pull` will not bring it back."
    )


def test_agent_context_preserves_quebec_wagering_boundary():
    context = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "Québec wagering boundary" in context
    assert "data/research application only" in context
    assert "Betfair's terms list Canada as a prohibited territory" in context
