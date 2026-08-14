import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.decisions import (
    DecisionPipeline,
    EvidenceClaim,
    ExecutionService,
    OrderSide,
    ProposalService,
    ResearchBrief,
    RiskEngine,
    RiskPolicy,
    RiskService,
    SourceRecord,
    TradingDecision,
)
from backend.decisions.models import ProposedTrade
from backend.decisions.repository import DecisionRepository
from backend.market.models import DataMode, MarketObservation, ObservationSource
from backend.research import ResearchPolicy

NOW = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)


def source(**updates) -> SourceRecord:
    values = dict(
        source_id="s1",
        canonical_url="https://news.example.com/story?utm_source=feed#top",
        publisher="Example News",
        title="Company reports results",
        published_at=NOW - timedelta(hours=2),
        retrieved_at=NOW - timedelta(hours=1),
        supporting_excerpt="The company reported revenue and operating results.",
    )
    values.update(updates)
    return SourceRecord(**values)


def claim(**updates) -> EvidenceClaim:
    values = dict(
        claim_id="c1",
        claim="The company reported current operating results.",
        source_ids=["s1"],
        stance="supports",
        confidence=Decimal("0.80"),
    )
    values.update(updates)
    return EvidenceClaim(**values)


def brief(**updates) -> ResearchBrief:
    values = dict(
        summary="Concise cited findings.",
        as_of=NOW,
        sources=[source()],
        claims=[claim()],
    )
    values.update(updates)
    return ResearchBrief(**values)


@pytest.mark.parametrize(
    "claims",
    [
        [claim(source_ids=[])],
        [claim(source_ids=["missing"])],
    ],
)
def test_missing_and_broken_citations_are_rejected(claims) -> None:
    with pytest.raises(ValidationError):
        brief(claims=claims)


def test_duplicate_url_is_canonicalized_and_rejected() -> None:
    duplicate = source(
        source_id="s2",
        canonical_url="https://NEWS.example.com/story/",
        supporting_excerpt="A distinct excerpt from the same article.",
    )
    with pytest.raises(ValidationError, match="duplicate article"):
        brief(sources=[source(), duplicate])


def test_duplicate_content_and_conflicting_publication_dates_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate article content"):
        brief(
            sources=[
                source(),
                source(source_id="s2", canonical_url="https://example.org/other"),
            ]
        )
    conflicting = source(
        source_id="s2",
        canonical_url="https://news.example.com/story",
        published_at=NOW - timedelta(hours=3),
        supporting_excerpt="Different excerpt.",
    )
    with pytest.raises(ValidationError, match="conflicting publication dates"):
        brief(sources=[source(), conflicting])


def test_research_brief_bounds_sources_and_excerpts() -> None:
    with pytest.raises(ValidationError):
        source(supporting_excerpt="x" * 241)

    sources = [
        source(
            source_id=f"s{index}",
            canonical_url=f"https://example.com/article-{index}",
            supporting_excerpt=f"Distinct supporting excerpt {index}.",
        )
        for index in range(1, 7)
    ]
    with pytest.raises(ValidationError):
        brief(sources=sources)


def test_future_dated_source_and_unsupported_proposal_are_rejected() -> None:
    future = source(
        published_at=NOW + timedelta(minutes=1), retrieved_at=NOW + timedelta(minutes=2)
    )
    with pytest.raises(ValidationError, match="future-dated"):
        brief(sources=[future])
    with pytest.raises(ValidationError, match="unknown evidence claim"):
        TradingDecision(
            research=brief(),
            appraisal="No private reasoning.",
            proposals=[
                ProposedTrade(
                    symbol="AAPL",
                    side="buy",
                    quantity=1,
                    sector="technology",
                    rationale="Unsupported recommendation",
                    evidence_claim_ids=["missing"],
                )
            ],
        )

    unsupported = claim(material=False, source_ids=[])
    with pytest.raises(ValidationError, match="unsupported evidence"):
        TradingDecision(
            research=brief(claims=[unsupported]),
            appraisal="No private reasoning.",
            proposals=[
                ProposedTrade(
                    symbol="AAPL",
                    side="buy",
                    quantity=1,
                    sector="technology",
                    rationale="Unsupported recommendation",
                    evidence_claim_ids=["c1"],
                )
            ],
        )


def test_domain_allow_and_deny_policy() -> None:
    ResearchPolicy(allowed_domains=frozenset({"example.com"})).validate(brief())
    with pytest.raises(ValueError, match="denied"):
        ResearchPolicy(denied_domains=frozenset({"example.com"})).validate(brief())
    with pytest.raises(ValueError, match="not allowed"):
        ResearchPolicy(allowed_domains=frozenset({"trusted.test"})).validate(brief())


def test_research_schema_and_database_round_trip_with_full_evidence_chain(
    tmp_path,
) -> None:
    repo = DecisionRepository(str(tmp_path / "evidence.db"))
    observation = MarketObservation(
        symbol="AAPL",
        price=Decimal("100"),
        currency="USD",
        market_timestamp=NOW,
        retrieved_at=NOW,
        source=ObservationSource.SIMULATOR,
        mode=DataMode.SIMULATED,
        is_stale=False,
        provider_endpoint="test/v1",
    )
    policy = RiskPolicy(
        max_position_percentage=Decimal("1"),
        max_symbol_concentration=Decimal("1"),
        max_sector_concentration=Decimal("1"),
        minimum_cash_reserve=Decimal("0"),
        maximum_order_notional=Decimal("100000"),
        maximum_daily_turnover=Decimal("100000"),
        maximum_drawdown=Decimal("1"),
    )
    output = TradingDecision(
        research=brief(),
        appraisal="Cited paper proposal.",
        trader_prompt_version="trader-test-v2",
        proposals=[
            ProposedTrade(
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=1,
                sector="technology",
                rationale="The cited update supports review.",
                evidence_claim_ids=["c1"],
            )
        ],
    )
    proposal, _, _ = DecisionPipeline(
        ProposalService(repo, lambda _: observation, clock=lambda: NOW),
        RiskService(repo, RiskEngine(policy), lambda _: observation, clock=lambda: NOW),
        ExecutionService(repo, clock=lambda: NOW),
    ).process("Alice", output)[0]
    evidence = repo.evidence_chain(str(proposal.proposal_id))
    assert ResearchBrief.model_validate(evidence["research"]) == output.research
    assert evidence["research"]["schema_version"] == "1.0"
    assert evidence["proposal"]["evidence_claim_ids"] == ["c1"]
    assert evidence["prompt_versions"] == {
        "researcher": "researcher-v1",
        "trader": "trader-test-v2",
    }
    assert evidence["market_observation"]["source"] == "simulator"
    with sqlite3.connect(repo.db_path) as conn:
        stored = conn.execute("SELECT brief FROM research_briefs").fetchone()[0]
    assert json.loads(stored)["sources"][0]["content_hash"]


def test_zero_proposal_run_still_persists_research_and_prompt_versions(
    tmp_path,
) -> None:
    repo = DecisionRepository(str(tmp_path / "no-trade.db"))
    service = ProposalService(repo, lambda _: None, clock=lambda: NOW)
    output = TradingDecision(research=brief(), proposals=[], appraisal="No trade warranted.")
    DecisionPipeline(service, None, None).process(
        "Alice", output
    )  # unused services for a no-trade run
    with sqlite3.connect(repo.db_path) as conn:
        row = conn.execute(
            "SELECT researcher_prompt_version, trader_prompt_version FROM research_briefs"
        ).fetchone()
    assert row == ("researcher-v1", "trader-v1")
