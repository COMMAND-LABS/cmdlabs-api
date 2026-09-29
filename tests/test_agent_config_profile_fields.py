"""description and starterPrompts are optional, user-facing agent config fields.

They are shown in the UI (agent picker, chat start screen) and never sent to
the model, so the schema must accept them and bound their size.
"""
import pytest
from jsonschema import ValidationError

from src.schemas import validate_against_schema


def _config(**data):
    return {
        "schema": "agent_config",
        "version": 4,
        "data": {"systemPrompt": "You are a helpful assistant.", **data},
    }


def test_description_and_starter_prompts_validate():
    validate_against_schema(
        _config(
            description="Forecasts duty spend from your import data.",
            starterPrompts=[
                "What's my expected duty spend next month?",
                "How would a 25% rate on my top category change next quarter?",
            ],
        ),
        "agent_config",
        4,
    )


def test_both_fields_are_optional():
    validate_against_schema(_config(), "agent_config", 4)


def test_more_than_six_starter_prompts_fails():
    with pytest.raises(ValidationError):
        validate_against_schema(
            _config(starterPrompts=[f"Question {i}?" for i in range(7)]),
            "agent_config",
            4,
        )


def test_overlong_description_fails():
    with pytest.raises(ValidationError):
        validate_against_schema(_config(description="x" * 501), "agent_config", 4)


def test_overlong_starter_prompt_fails():
    with pytest.raises(ValidationError):
        validate_against_schema(_config(starterPrompts=["x" * 201]), "agent_config", 4)
