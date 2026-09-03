import pytest

from backend.trading_floor import (
    DEFAULT_MODEL_NAME,
    DEFAULT_RESEARCH_MODEL_NAME,
    configured_model_name,
)


def test_model_name_defaults_to_current_model() -> None:
    assert configured_model_name(None) == DEFAULT_MODEL_NAME == "gpt-5.4-mini"


def test_research_model_defaults_to_pinned_non_reasoning_model() -> None:
    assert (
        configured_model_name(None, default=DEFAULT_RESEARCH_MODEL_NAME)
        == DEFAULT_RESEARCH_MODEL_NAME
        == "gpt-4.1-mini-2025-04-14"
    )


def test_traders_receive_separate_research_model(monkeypatch) -> None:
    import backend.trading_floor as floor

    monkeypatch.setattr(floor, "ensure_default_strategies", lambda: None)
    monkeypatch.setattr(floor, "RESEARCH_MODEL_NAME", DEFAULT_RESEARCH_MODEL_NAME)

    traders = floor.create_traders()

    assert {trader.model_name for trader in traders} == {"gpt-5.4-mini"}
    assert {trader.research_model_name for trader in traders} == {
        "gpt-4.1-mini-2025-04-14"
    }


def test_model_name_preserves_supported_provider_identifier_shapes() -> None:
    assert configured_model_name("  gpt-5.4-mini  ") == "gpt-5.4-mini"
    assert configured_model_name("openai/gpt-5.4-mini") == "openai/gpt-5.4-mini"


@pytest.mark.parametrize("value", ["", "   ", "model name", "model?$bad", "x" * 101])
def test_invalid_model_name_fails_configuration(value: str) -> None:
    with pytest.raises(ValueError, match="MODEL_NAME"):
        configured_model_name(value)
