from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agent_context_preserves_quebec_wagering_boundary():
    context = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "Québec wagering boundary" in context
    assert "data/research application only" in context
    assert "Betfair's terms list Canada as a prohibited territory" in context
