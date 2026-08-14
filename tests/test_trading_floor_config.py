import pytest

from backend.trading_floor import DEFAULT_MODEL_NAME, configured_model_name


def test_model_name_defaults_to_current_model() -> None:
    assert configured_model_name(None) == DEFAULT_MODEL_NAME == "gpt-5.4-mini"


def test_model_name_preserves_supported_provider_identifier_shapes() -> None:
    assert configured_model_name("  gpt-5.4-mini  ") == "gpt-5.4-mini"
    assert configured_model_name("openai/gpt-5.4-mini") == "openai/gpt-5.4-mini"


@pytest.mark.parametrize("value", ["", "   ", "model name", "model?$bad", "x" * 101])
def test_invalid_model_name_fails_configuration(value: str) -> None:
    with pytest.raises(ValueError, match="MODEL_NAME"):
        configured_model_name(value)
