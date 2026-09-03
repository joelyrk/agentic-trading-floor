import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from agents import MaxTurnsExceeded, ModelBehaviorError, Usage
from openai import BadRequestError

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


def detailed_usage(input_tokens: int, output_tokens: int) -> Usage:
    return Usage(
        requests=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def test_cycle_cost_prices_research_and_trader_usage_separately() -> None:
    budget = CycleBudget(
        input_cost_per_million=Decimal("2"),
        output_cost_per_million=Decimal("4"),
        research_input_cost_per_million=Decimal("0.4"),
        research_output_cost_per_million=Decimal("1.6"),
    )
    trader = traders.Trader("Warren")
    trader._research_usage = detailed_usage(100, 50)
    combined = detailed_usage(300, 150)

    assert trader._cycle_cost(budget, combined) == Decimal("0.00092")


def test_cycle_cost_prices_partial_research_failure_at_research_rates() -> None:
    budget = CycleBudget(
        input_cost_per_million=Decimal("2"),
        output_cost_per_million=Decimal("4"),
        research_input_cost_per_million=Decimal("0.4"),
        research_output_cost_per_million=Decimal("1.6"),
    )
    trader = traders.Trader("Warren")
    trader._active_stage = "research"

    assert trader._cycle_cost(budget, detailed_usage(100, 50)) == Decimal("0.00012")


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
                        response_id=f"resp-{calls}",
                        request_id=f"req-{calls}",
                        raw_usage=None,
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
            r"\(transport=object, output_types=reasoning, "
            r"usage_status=unavailable, responses=\["
            r"1:response=resp-1,request=req-1,types=reasoning,tokens=0,raw_usage=missing;"
            r"2:response=resp-2,request=req-2,types=reasoning,tokens=0,raw_usage=missing\]\)"
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


def test_openai_researcher_uses_pinned_bounded_chat_contract(monkeypatch) -> None:
    client = traders.AsyncOpenAI(api_key="test-key")
    monkeypatch.setattr(traders, "_openai_client", lambda: client)

    agent = traders.get_researcher("gpt-4.1-mini-2025-04-14", NOW)

    assert isinstance(agent.model, traders.OpenAIChatCompletionsModel)
    assert isinstance(traders.get_model("gpt-5.4-mini"), traders.OpenAIResponsesModel)
    assert agent.model_settings.max_tokens is None
    assert agent.model_settings.extra_args == {"max_completion_tokens": 2_500}
    assert agent.model_settings.reasoning is None
    assert agent.model_settings.verbosity is None
    assert agent.output_type is traders.ResearchSynthesisOutput
    assert traders.RESEARCH_MAX_TURNS == 1


def test_optional_provider_researcher_uses_same_bounded_contract(monkeypatch) -> None:
    client = traders.AsyncOpenAI(api_key="test-key")
    monkeypatch.setattr(traders, "_optional_client", lambda provider: client)

    agent = traders.get_researcher("deepseek-chat", NOW)

    assert isinstance(agent.model, traders.OpenAIChatCompletionsModel)
    assert agent.model_settings.max_tokens == 2_500
    assert agent.model_settings.extra_args is None


def test_output_limit_error_is_attributable_and_marks_usage_unavailable(monkeypatch) -> None:
    calls = 0
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        headers={"x-request-id": "req-limit"},
    )
    provider_error = BadRequestError(
        "Could not finish the message because max_tokens or model output limit was reached.",
        response=response,
        body={"error": {"message": "output limit was reached"}},
    )

    async def run(agent, message, **kwargs):
        nonlocal calls
        calls += 1
        raise provider_error

    monkeypatch.setattr(traders.Runner, "run", run)
    trader = traders.Trader("Warren")
    monkeypatch.setattr(trader, "_log_stage", lambda message: None)
    agent = SimpleNamespace(
        model=SimpleNamespace(model="gpt-5.4-mini"),
        model_settings=traders.ModelSettings(
            max_tokens=2_500,
        ),
    )

    with pytest.raises(
        traders.ProviderOutputLimitError,
        match=(
            r"research output_limit_exceeded .*max_tokens=2500, "
            r"reasoning_effort=provider-default, request_id=req-limit, "
            r"usage_status=unavailable"
        ),
    ):
        asyncio.run(
            trader._run_structured_stage(
                agent,
                "request",
                stage_name="research",
                stage_budget=CycleBudget(max_tokens=40_000),
                max_turns=2,
            )
        )

    assert calls == 1
    assert trader._last_usage.requests == 1
    assert trader._last_usage.total_tokens == 0


def test_max_turn_error_handler_prefers_completed_response_token_usage() -> None:
    hooks = traders.BudgetHooks(CycleBudget(max_tokens=100))
    handler_input = SimpleNamespace(
        context=SimpleNamespace(usage=Usage(requests=2)),
        run_data=SimpleNamespace(
            raw_responses=[
                SimpleNamespace(
                    usage=usage(20),
                    output=[
                        SimpleNamespace(
                            type="message",
                            provider_data={"response_id": "chatcmpl-1"},
                        )
                    ],
                    response_id=None,
                    request_id="req-1",
                    raw_usage={"total_tokens": 20},
                ),
                SimpleNamespace(
                    usage=usage(30),
                    output=[],
                    response_id="resp-2",
                    request_id="req-2",
                    raw_usage=None,
                ),
            ]
        ),
    )

    asyncio.run(hooks.capture_run_error(handler_input))

    assert hooks.usage.requests == 2
    assert hooks.usage.input_tokens == 40
    assert hooks.usage.output_tokens == 10
    assert hooks.usage.total_tokens == 50
    assert [item.compact(index) for index, item in enumerate(hooks.response_diagnostics, 1)] == [
        "1:response=chatcmpl-1,request=req-1,types=message,tokens=20,raw_usage=available",
        "2:response=resp-2,request=req-2,types=none,tokens=30,raw_usage=missing",
    ]


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
