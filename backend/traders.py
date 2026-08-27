import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from functools import lru_cache
from time import monotonic

from agents import (
    Agent,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    Runner,
    Usage,
    gen_trace_id,
    trace,
)
from dotenv import load_dotenv
from openai import AsyncOpenAI

from .accounts_client import read_account_snapshot_resource, read_strategy_resource
from .database import write_log
from .decisions import (
    DecisionPipeline,
    ExecutionService,
    ProposalService,
    ResearchBrief,
    RiskEngine,
    RiskPolicy,
    RiskService,
    SourceRecord,
    TraderRecommendation,
)
from .decisions.repository import DecisionRepository
from .market import get_market_observation, get_market_service
from .mcp_servers import (
    attribute_runtime_failure,
    researcher_mcp_servers,
    trader_mcp_servers,
)
from .observability import (
    BudgetExceeded,
    BudgetHooks,
    CycleBudget,
    CycleContext,
    TelemetryRepository,
    safe_error,
)
from .research import (
    RESEARCHER_PROMPT_VERSION,
    TRADER_PROMPT_VERSION,
    EvidenceClaim,
    ResearchPolicy,
    ResearchSynthesis,
)
from .research_search_server import BoundedSearchBundle
from .templates import (
    rebalance_message,
    research_message,
    researcher_instructions,
    trade_message,
    trader_instructions,
)

load_dotenv()

deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
google_api_key = os.getenv("GOOGLE_API_KEY")
grok_api_key = os.getenv("GROK_API_KEY")
openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
GROK_BASE_URL = "https://api.x.ai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL_MAX_RETRIES = int(os.getenv("MODEL_MAX_RETRIES", "4"))
MODEL_OUTPUT_REPAIR_ATTEMPTS = 1
RESEARCH_MAX_TURNS = 3


def trader_trace_metadata(
    *, name: str, context: CycleContext, model_name: str, market_mode: str
) -> dict[str, str]:
    """Build metadata accepted by OpenAI's trace ingest API.

    Trace metadata values must be strings. Decision IDs are persisted with the
    cycle telemetry after processing because they do not exist when the root
    trace is exported at context entry.
    """
    return {
        "agent_name": name.lower(),
        "cycle_id": context.cycle_id,
        "run_id": context.run_id or "live",
        "scenario_id": context.scenario_id or "live",
        "prompt_version": TRADER_PROMPT_VERSION,
        "market_mode": market_mode,
        "model": model_name,
        "sensitive_payload_capture": "false",
    }


@lru_cache(maxsize=1)
def _openai_client() -> AsyncOpenAI:
    """Official client honors Retry-After and applies bounded backoff with jitter."""
    return AsyncOpenAI(max_retries=MODEL_MAX_RETRIES)


@lru_cache(maxsize=4)
def _optional_client(provider: str) -> AsyncOpenAI:
    settings = {
        "openrouter": (OPENROUTER_BASE_URL, openrouter_api_key),
        "deepseek": (DEEPSEEK_BASE_URL, deepseek_api_key),
        "grok": (GROK_BASE_URL, grok_api_key),
        "gemini": (GEMINI_BASE_URL, google_api_key),
    }
    base_url, api_key = settings[provider]
    if not api_key:
        raise ValueError(f"{provider} model selected but its API key is not configured")
    return AsyncOpenAI(base_url=base_url, api_key=api_key)


def get_model(model_name: str):
    if "/" in model_name:
        return OpenAIChatCompletionsModel(
            model=model_name, openai_client=_optional_client("openrouter")
        )
    elif "deepseek" in model_name:
        return OpenAIChatCompletionsModel(
            model=model_name, openai_client=_optional_client("deepseek")
        )
    elif "grok" in model_name:
        return OpenAIChatCompletionsModel(model=model_name, openai_client=_optional_client("grok"))
    elif "gemini" in model_name:
        return OpenAIChatCompletionsModel(
            model=model_name, openai_client=_optional_client("gemini")
        )
    else:
        return OpenAIResponsesModel(model=model_name, openai_client=_openai_client())


def get_researcher(model_name: str, decision_cutoff: datetime) -> Agent:
    return Agent(
        name="Researcher",
        instructions=researcher_instructions(decision_cutoff),
        model=get_model(model_name),
        model_settings=ModelSettings(max_tokens=2_500, preserve_raw_usage=True),
        output_type=ResearchSynthesis,
    )


def bounded_account_report(account: dict) -> str:
    """Keep model context independent of an account's unbounded transaction history."""
    transactions = account.get("transactions")
    recent = transactions[-5:] if isinstance(transactions, list) else []
    compact_transactions = []
    for item in recent:
        if not isinstance(item, dict):
            continue
        compact_transactions.append(
            {
                "symbol": item.get("symbol"),
                "quantity": item.get("quantity"),
                "price": item.get("price"),
                "timestamp": item.get("timestamp"),
                "rationale": str(item.get("rationale", ""))[:240],
            }
        )
    return json.dumps(
        {
            "name": account.get("name"),
            "balance": account.get("balance"),
            "holdings": account.get("holdings", {}),
            "recent_transactions": compact_transactions,
        },
        separators=(",", ":"),
    )


def _search_query(name: str, strategy: str, account: str, now: datetime) -> str:
    return " ".join(
        (
            f"Latest material financial news relevant to {name}'s paper portfolio",
            f"and strategy as of {now.date().isoformat()}.",
            f"Strategy: {' '.join(strategy.split())[:260]}.",
            f"Account: {account[:140]}.",
        )
    )[:500]


async def _bounded_search(server, query: str) -> BoundedSearchBundle:
    result = await server.call_tool("search", {"query": query})
    if getattr(result, "isError", False):
        raise RuntimeError("bounded research search returned an error")
    text = next(
        (item.text for item in result.content if hasattr(item, "text") and item.text),
        None,
    )
    if text is None:
        raise RuntimeError("bounded research search returned no catalog")
    return BoundedSearchBundle.model_validate_json(text)


def _research_brief(
    bundle: BoundedSearchBundle,
    synthesis: ResearchSynthesis,
    decision_cutoff: datetime,
) -> ResearchBrief:
    sources = [
        SourceRecord(
            source_id=item.source_id,
            canonical_url=item.canonical_url,
            publisher=item.publisher,
            title=item.title,
            published_at=item.published_at,
            retrieved_at=item.retrieved_at,
            supporting_excerpt=item.snippet[:200],
            caveats=(
                [
                    "Publication time unavailable; retrieval time is the conservative availability bound."
                ]
                if item.publication_time_inferred
                else []
            ),
        )
        for item in bundle.results
    ]
    claims = [EvidenceClaim.model_validate(item.model_dump()) for item in synthesis.claims]
    brief = ResearchBrief(
        summary=synthesis.summary,
        as_of=decision_cutoff,
        sources=sources,
        claims=claims,
        caveats=synthesis.caveats,
        researcher_prompt_version=RESEARCHER_PROMPT_VERSION,
    )
    ResearchPolicy.from_env().validate(brief)
    return brief


def trader_research_context(research: ResearchBrief) -> str:
    """Expose only material, cited claim IDs to the proposal-generating model."""
    eligible_claims = [claim for claim in research.claims if claim.material and claim.source_ids]
    eligible_source_ids = {source_id for claim in eligible_claims for source_id in claim.source_ids}
    return json.dumps(
        {
            "summary": research.summary,
            "as_of": research.as_of.isoformat(),
            "eligible_evidence_claim_ids": [claim.claim_id for claim in eligible_claims],
            "claims": [claim.model_dump(mode="json") for claim in eligible_claims],
            "sources": [
                source.model_dump(mode="json")
                for source in research.sources
                if source.source_id in eligible_source_ids
            ],
            "caveats": research.caveats,
        },
        separators=(",", ":"),
    )


def only_skippable_proposal_errors(error: str | None) -> bool:
    """Identify proposal-local evidence or market-data failures that should not fail a cycle."""
    return bool(error) and all(
        ": market_data_unavailable:" in item or ": evidence_rejection:" in item
        for item in error.split("; ")
    )


class Trader:
    def __init__(self, name: str, lastname="Trader", model_name="gpt-5.4-mini"):
        self.name = name
        self.lastname = lastname
        self.agent = None
        self.model_name = model_name
        self.do_trade = True
        self._budget_hooks = None
        self._last_usage = None
        self._run_id: str | None = None
        self._processing_warning: str | None = None

    def _log_stage(self, message: str) -> None:
        run_label = self._run_id or "uncoordinated"
        write_log(self.name, "cycle", f"Run {run_label}: {message}")

    def _failure_usage(self):
        """Preserve completed-stage and partial current-stage usage without double counting."""
        current = self._budget_hooks.usage if self._budget_hooks is not None else None
        if self._last_usage is None:
            return current
        if current is None:
            return self._last_usage
        combined = Usage()
        combined.add(self._last_usage)
        combined.add(current)
        return combined

    @staticmethod
    def _combined_usage(prior: Usage | None, current: Usage | None) -> Usage:
        combined = Usage()
        if prior is not None:
            combined.add(prior)
        if current is not None:
            combined.add(current)
        return combined

    async def _run_structured_stage(
        self,
        agent: Agent,
        message: str,
        *,
        stage_name: str,
        stage_budget: CycleBudget,
        max_turns: int,
        prior_usage: Usage | None = None,
    ) -> tuple[object, Usage]:
        """Retry incomplete structured output once while preserving usage and budgets."""
        stage_usage = Usage()
        active_message = message
        for repair_attempt in range(MODEL_OUTPUT_REPAIR_ATTEMPTS + 1):
            spent = stage_budget.estimate_cost(stage_usage.input_tokens, stage_usage.output_tokens)
            remaining_tokens = stage_budget.max_tokens - stage_usage.total_tokens
            remaining_spend = stage_budget.max_spend_usd - spent
            if remaining_tokens <= 0 or remaining_spend <= 0:
                raise BudgetExceeded(f"{stage_name} repair exhausted the cycle budget")
            attempt_budget = stage_budget.model_copy(
                update={
                    "max_tokens": remaining_tokens,
                    "max_spend_usd": remaining_spend,
                }
            )
            self._budget_hooks = BudgetHooks(attempt_budget)
            try:
                result = await Runner.run(
                    agent,
                    active_message,
                    max_turns=max_turns,
                    hooks=self._budget_hooks,
                    run_config={"trace_include_sensitive_data": False},
                    error_handlers={
                        "max_turns": self._budget_hooks.capture_run_error,
                        "invalid_final_output": self._budget_hooks.capture_run_error,
                    },
                )
            except (MaxTurnsExceeded, ModelBehaviorError):
                if self._budget_hooks.usage is not None:
                    stage_usage.add(self._budget_hooks.usage)
                self._last_usage = self._combined_usage(prior_usage, stage_usage)
                self._budget_hooks = None
                if repair_attempt >= MODEL_OUTPUT_REPAIR_ATTEMPTS:
                    raise
                self._log_stage(f"repairing incomplete {stage_name} structured output")
                active_message = (
                    message + "\nThe previous attempt did not produce valid final structured output. "
                    "Return a fresh response that exactly matches the required schema. "
                    "Do not add prose outside the structured response."
                )
                continue
            stage_usage.add(result.context_wrapper.usage)
            self._last_usage = self._combined_usage(prior_usage, stage_usage)
            self._budget_hooks = None
            return result, stage_usage
        raise AssertionError("structured repair loop did not return")

    async def create_agent(self, trader_mcp_servers, decision_cutoff: datetime) -> Agent:
        self.agent = Agent(
            name=self.name,
            instructions=trader_instructions(self.name, decision_cutoff),
            model=get_model(self.model_name),
            model_settings=ModelSettings(max_tokens=2_000, preserve_raw_usage=True),
            mcp_servers=trader_mcp_servers,
            output_type=TraderRecommendation,
        )
        return self.agent

    async def get_account_report(self) -> str:
        account = await read_account_snapshot_resource(self.name)
        account_json = json.loads(account)
        return bounded_account_report(account_json)

    async def run_agent(self, trader_mcp_servers, research_search_server, budget: CycleBudget):
        self._log_stage("reading the paper account and strategy")
        account = await self.get_account_report()
        strategy = " ".join((await read_strategy_resource(self.name)).split())[:2_000]
        search_started = datetime.now(timezone.utc)
        self._log_stage("searching a bounded recent-news catalog (maximum 5 snippets)")
        bundle = await _bounded_search(
            research_search_server,
            _search_query(self.name, strategy, account, search_started),
        )
        decision_cutoff = datetime.now(timezone.utc)
        researcher = get_researcher(self.model_name, decision_cutoff)
        self._log_stage(f"synthesizing evidence from {len(bundle.results)} bounded sources")
        research_result, research_usage = await self._run_structured_stage(
            researcher,
            research_message(
                strategy,
                account,
                bundle.model_dump_json(),
                decision_cutoff,
            ),
            stage_name="research",
            stage_budget=budget,
            max_turns=min(budget.max_turns, RESEARCH_MAX_TURNS),
        )
        if not isinstance(research_result.final_output, ResearchSynthesis):
            raise ValueError("researcher did not return ResearchSynthesis")
        research = _research_brief(bundle, research_result.final_output, decision_cutoff)
        spent = budget.estimate_cost(research_usage.input_tokens, research_usage.output_tokens)
        remaining_tokens = budget.max_tokens - research_usage.total_tokens
        remaining_spend = budget.max_spend_usd - spent
        if remaining_tokens <= 0 or remaining_spend <= 0:
            raise BudgetExceeded("research stage exhausted the cycle budget")
        remaining_budget = budget.model_copy(
            update={"max_tokens": remaining_tokens, "max_spend_usd": remaining_spend}
        )
        self.agent = await self.create_agent(trader_mcp_servers, decision_cutoff)
        research_json = trader_research_context(research)
        message = (
            trade_message(self.name, strategy, account, research_json, decision_cutoff)
            if self.do_trade
            else rebalance_message(self.name, strategy, account, research_json, decision_cutoff)
        )
        self._log_stage("evaluating the strategy against validated evidence")
        result, trader_usage = await self._run_structured_stage(
            self.agent,
            message,
            stage_name="trader",
            stage_budget=remaining_budget,
            max_turns=min(budget.max_turns, 5),
            prior_usage=research_usage,
        )
        usage = Usage()
        usage.add(research_usage)
        usage.add(trader_usage)
        self._last_usage = usage
        estimated_cost = budget.estimate_cost(usage.input_tokens, usage.output_tokens)
        if usage.total_tokens > budget.max_tokens:
            raise BudgetExceeded(
                f"cycle token budget exceeded ({usage.total_tokens}/{budget.max_tokens})"
            )
        if estimated_cost > budget.max_spend_usd:
            raise BudgetExceeded(
                f"cycle spend budget exceeded ({estimated_cost}/{budget.max_spend_usd} USD)"
            )
        repository = DecisionRepository()
        proposal_service = ProposalService(repository, get_market_observation)
        risk_service = RiskService(
            repository, RiskEngine(RiskPolicy.from_env()), get_market_observation
        )
        pipeline = DecisionPipeline(proposal_service, risk_service, ExecutionService(repository))
        recommendation = result.final_output
        if not isinstance(recommendation, TraderRecommendation):
            raise ValueError("trader did not return TraderRecommendation")
        self._log_stage(
            f"applying deterministic risk controls to {len(recommendation.proposals)} proposals"
        )
        processed, error = pipeline.safely_process_recommendation(
            self.name,
            research,
            recommendation,
            TRADER_PROMPT_VERSION,
        )
        skippable_only = only_skippable_proposal_errors(error)
        if error and not processed and not skippable_only:
            raise ValueError(error)
        self._processing_warning = error
        if error:
            failed_count = len(recommendation.proposals) - len(processed)
            self._log_stage(
                f"completed with {len(processed)} processed and {failed_count} skipped proposal; "
                f"warning: {safe_error(error)}"
            )
        else:
            self._log_stage(f"completed with {len(processed)} processed paper proposals")
        return processed, usage

    async def run_with_mcp_servers(self, budget: CycleBudget):
        async with AsyncExitStack() as stack:
            trader_servers = [
                await stack.enter_async_context(server) for server in trader_mcp_servers()
            ]
            research_servers = [
                await stack.enter_async_context(server)
                for server in researcher_mcp_servers(self.name)
            ]
            if len(research_servers) != 1:
                raise RuntimeError("exactly one bounded research search server is required")
            return await self.run_agent(trader_servers, research_servers[0], budget)

    async def run_with_trace(self, context: CycleContext, budget: CycleBudget):
        trace_name = f"{self.name}-trading" if self.do_trade else f"{self.name}-rebalancing"
        trace_id = gen_trace_id()
        metadata = trader_trace_metadata(
            name=self.name,
            context=context,
            model_name=self.model_name,
            market_mode=get_market_service().status().mode.value,
        )
        with trace(trace_name, trace_id=trace_id, group_id=context.run_id, metadata=metadata):
            processed, usage = await self.run_with_mcp_servers(budget)
            return processed, usage, trace_id

    async def run(self, *, run_id: str | None = None):
        self._budget_hooks = None
        self._last_usage = None
        self._processing_warning = None
        budget = CycleBudget.from_env()
        context = CycleContext.create(
            run_id=run_id or os.getenv("EVALUATION_RUN_ID"),
            scenario_id=os.getenv("EVALUATION_SCENARIO_ID"),
        )
        self._run_id = context.run_id
        telemetry = TelemetryRepository()
        market_mode = get_market_service().status().mode.value
        telemetry.start_cycle(
            context,
            self.name,
            self.model_name,
            TRADER_PROMPT_VERSION,
            market_mode,
            budget,
        )
        started = monotonic()
        usage = None
        try:
            async with asyncio.timeout(budget.max_wall_seconds):
                processed, usage, trace_id = await self.run_with_trace(context, budget)
            cost = budget.estimate_cost(usage.input_tokens, usage.output_tokens)
            telemetry.finish_cycle(
                context.cycle_id,
                status="succeeded",
                usage=usage,
                latency_ms=(monotonic() - started) * 1000,
                estimated_cost=cost,
                error=self._processing_warning,
                decision_ids=[str(decision.decision_id) for _, decision, _ in processed],
                trace_id=trace_id,
            )
        except asyncio.CancelledError:
            usage = self._failure_usage()
            self._log_stage("interrupted during scheduler shutdown")
            cost = budget.estimate_cost(
                getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0)
            )
            telemetry.finish_cycle(
                context.cycle_id,
                status="interrupted",
                usage=usage,
                latency_ms=(monotonic() - started) * 1000,
                estimated_cost=cost,
                error="scheduler shutdown interrupted cycle",
            )
            raise
        except Exception as e:
            usage = self._failure_usage()
            self._log_stage(f"failed: {safe_error(e)}")
            attribute_runtime_failure(e)
            cost = budget.estimate_cost(
                getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0)
            )
            telemetry.finish_cycle(
                context.cycle_id,
                status="failed",
                usage=usage,
                latency_ms=(monotonic() - started) * 1000,
                estimated_cost=cost,
                error=e,
            )
            print(f"Error running trader {self.name}: {e}")
        self.do_trade = not self.do_trade
