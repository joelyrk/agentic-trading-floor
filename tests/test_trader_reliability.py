import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from agents import MaxTurnsExceeded, ModelBehaviorError, Usage

import backend.traders as traders
from backend.decisions import EvidenceClaim, ResearchBrief, SourceRecord
from backend.observability import CycleBudget

NOW = datetime(2026, 8, 22, 9, tzinfo=timezone.utc)


def usage(tokens: int) -> Usage:
    return Usage(
        requests=1,
        input_tokens=tokens - 5,
        output_tokens=5,
        total_tokens=tokens,
    )


def test_structured_stage_repairs_one_malformed_response(monkeypatch) -> None:
    calls = []

    async def run(agent, message, **kwargs):
        calls.append(message)
        if len(calls) == 1:
            kwargs["hooks"].usage = usage(20)
            raise ModelBehaviorError("Invalid JSON when parsing model output")
        return SimpleNamespace(
            final_output="valid",
            context_wrapper=SimpleNamespace(usage=usage(30)),
        )

    monkeypatch.setattr(traders.Runner, "run", run)
    trader = traders.Trader("Warren")
    monkeypatch.setattr(trader, "_log_stage", lambda message: None)

    result, combined = asyncio.run(
        trader._run_structured_stage(
            object(),
            "original request",
            stage_name="research",
            stage_budget=CycleBudget(max_tokens=100),
            max_turns=2,
        )
    )

    assert result.final_output == "valid"
    assert len(calls) == 2
    assert calls[0] == "original request"
    assert "did not produce valid final structured output" in calls[1]
    assert combined.requests == 2
    assert combined.total_tokens == 50
    assert trader._last_usage.total_tokens == 50


def test_structured_stage_stops_after_one_repair(monkeypatch) -> None:
    calls = 0

    async def run(agent, message, **kwargs):
        nonlocal calls
        calls += 1
        kwargs["hooks"].usage = usage(10)
        raise ModelBehaviorError("Invalid JSON when parsing model output")

    monkeypatch.setattr(traders.Runner, "run", run)
    trader = traders.Trader("George")
    monkeypatch.setattr(trader, "_log_stage", lambda message: None)

    with pytest.raises(ModelBehaviorError, match="Invalid JSON"):
        asyncio.run(
            trader._run_structured_stage(
                object(),
                "original request",
                stage_name="trader",
                stage_budget=CycleBudget(max_tokens=100),
                max_turns=5,
            )
        )

    assert calls == 2
    assert trader._last_usage.total_tokens == 20


def test_structured_stage_repairs_turn_exhaustion(monkeypatch) -> None:
    calls = []

    async def run(agent, message, **kwargs):
        calls.append(message)
        if len(calls) == 1:
            handler_input = SimpleNamespace(
                context=SimpleNamespace(usage=Usage(requests=1)),
                run_data=SimpleNamespace(raw_responses=[SimpleNamespace(usage=usage(25))]),
            )
            await kwargs["error_handlers"]["max_turns"](handler_input)
            raise MaxTurnsExceeded("Max turns (3) exceeded")
        return SimpleNamespace(
            final_output="valid",
            context_wrapper=SimpleNamespace(usage=usage(35)),
        )

    monkeypatch.setattr(traders.Runner, "run", run)
    trader = traders.Trader("Ray")
    monkeypatch.setattr(trader, "_log_stage", lambda message: None)

    result, combined = asyncio.run(
        trader._run_structured_stage(
            object(),
            "original request",
            stage_name="research",
            stage_budget=CycleBudget(max_tokens=100),
            max_turns=3,
        )
    )

    assert result.final_output == "valid"
    assert len(calls) == 2
    assert combined.requests == 2
    assert combined.total_tokens == 60
    assert trader._last_usage.total_tokens == 60


def test_structured_stage_bounds_repeated_empty_responses_with_diagnostic(monkeypatch) -> None:
    calls = 0

    async def run(agent, message, **kwargs):
        nonlocal calls
        calls += 1
        handler_input = SimpleNamespace(
            context=SimpleNamespace(usage=Usage(requests=1)),
            run_data=SimpleNamespace(
                raw_responses=[
                    SimpleNamespace(
                        usage=Usage(requests=1),
                        output=[SimpleNamespace(type="reasoning")],
                    )
                ]
            ),
        )
        await kwargs["error_handlers"]["max_turns"](handler_input)
        raise MaxTurnsExceeded("Max turns (1) exceeded")

    monkeypatch.setattr(traders.Runner, "run", run)
    trader = traders.Trader("Cathie")
    monkeypatch.setattr(trader, "_log_stage", lambda message: None)

    with pytest.raises(
        traders.IncompleteStructuredOutputError,
        match=(
            r"research incomplete_output after 2 model requests "
            r"\(output_types=reasoning, usage_status=unavailable\)"
        ),
    ):
        asyncio.run(
            trader._run_structured_stage(
                object(),
                "original request",
                stage_name="research",
                stage_budget=CycleBudget(max_tokens=100),
                max_turns=1,
            )
        )

    assert calls == 2
    assert trader._last_usage.requests == 2
    assert trader._last_usage.total_tokens == 0


def test_researcher_uses_bounded_low_verbosity_responses_settings(monkeypatch) -> None:
    model = traders.OpenAIResponsesModel(
        model="gpt-5.4-mini",
        openai_client=traders.AsyncOpenAI(api_key="test-key"),
    )
    monkeypatch.setattr(traders, "get_model", lambda model_name: model)

    agent = traders.get_researcher("gpt-5.4-mini", NOW)

    assert agent.model is model
    assert agent.model_settings.max_tokens == 8_000
    assert agent.model_settings.reasoning.effort == "none"
    assert agent.model_settings.verbosity == "low"
    assert traders.RESEARCH_MAX_TURNS == 1


def test_max_turn_error_handler_prefers_completed_response_token_usage() -> None:
    hooks = traders.BudgetHooks(CycleBudget(max_tokens=100))
    handler_input = SimpleNamespace(
        context=SimpleNamespace(usage=Usage(requests=2)),
        run_data=SimpleNamespace(
            raw_responses=[
                SimpleNamespace(usage=usage(20)),
                SimpleNamespace(usage=usage(30)),
            ]
        ),
    )

    asyncio.run(hooks.capture_run_error(handler_input))

    assert hooks.usage.requests == 2
    assert hooks.usage.input_tokens == 40
    assert hooks.usage.output_tokens == 10
    assert hooks.usage.total_tokens == 50


def test_trader_account_context_uses_non_valuing_snapshot(monkeypatch) -> None:
    calls = []

    async def read_snapshot(name: str) -> str:
        calls.append(name)
        return json.dumps(
            {
                "name": "warren",
                "balance": 7500.0,
                "strategy": "Value strategy",
                "holdings": {"BRK.B": 5},
                "transactions": [],
                "portfolio_value_time_series": [],
            }
        )

    monkeypatch.setattr(traders, "read_account_snapshot_resource", read_snapshot)

    report = json.loads(asyncio.run(traders.Trader("Warren").get_account_report()))

    assert calls == ["Warren"]
    assert report == {
        "name": "warren",
        "balance": 7500.0,
        "holdings": {"BRK.B": 5},
        "recent_transactions": [],
    }


def test_trader_context_exposes_only_eligible_claim_ids() -> None:
    source = SourceRecord(
        source_id="s1",
        canonical_url="https://example.com/story",
        publisher="Example",
        title="Story",
        published_at=NOW,
        retrieved_at=NOW,
        supporting_excerpt="A bounded supporting excerpt.",
    )
    eligible = EvidenceClaim(
        claim_id="c1",
        claim="A material supported claim.",
        source_ids=["s1"],
        stance="supports",
        confidence=Decimal("0.8"),
    )
    ineligible = EvidenceClaim(
        claim_id="c2",
        claim="Context only.",
        source_ids=[],
        stance="context",
        confidence=Decimal("0.4"),
        material=False,
    )
    research = ResearchBrief(
        summary="summary",
        as_of=NOW,
        sources=[source],
        claims=[eligible, ineligible],
    )

    context = json.loads(traders.trader_research_context(research))

    assert context["eligible_evidence_claim_ids"] == ["c1"]
    assert [claim["claim_id"] for claim in context["claims"]] == ["c1"]
    assert [item["source_id"] for item in context["sources"]] == ["s1"]
    assert "c2" not in json.dumps(context)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("AAPL: evidence_rejection: unsupported", True),
        ("AAPL: market_data_unavailable: empty day", True),
        (
            "AAPL: evidence_rejection: unsupported; MSFT: market_data_unavailable: empty day",
            True,
        ),
        ("AAPL: execution_conflict: insufficient cash", False),
        (None, False),
    ],
)
def test_only_proposal_local_errors_are_skippable(error, expected) -> None:
    assert traders.only_skippable_proposal_errors(error) is expected
