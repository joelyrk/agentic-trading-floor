import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from functools import lru_cache
from time import monotonic

from agents import Agent, OpenAIChatCompletionsModel, OpenAIResponsesModel, Runner, Tool, trace
from dotenv import load_dotenv
from openai import AsyncOpenAI

from .accounts_client import read_accounts_resource, read_strategy_resource
from .decisions import (
    DecisionPipeline,
    ExecutionService,
    ProposalService,
    ResearchBrief,
    RiskEngine,
    RiskPolicy,
    RiskService,
    TradingDecision,
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
)
from .research import RESEARCHER_PROMPT_VERSION, TRADER_PROMPT_VERSION
from .templates import (
    rebalance_message,
    research_tool,
    researcher_instructions,
    trade_message,
    trader_instructions,
)
from .tracers import make_trace_id

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


async def get_researcher(mcp_servers, model_name, decision_cutoff: datetime) -> Agent:
    researcher = Agent(
        name="Researcher",
        instructions=researcher_instructions(decision_cutoff),
        model=get_model(model_name),
        mcp_servers=mcp_servers,
        output_type=ResearchBrief,
    )
    return researcher


async def get_researcher_tool(mcp_servers, model_name, decision_cutoff: datetime) -> Tool:
    researcher = await get_researcher(mcp_servers, model_name, decision_cutoff)
    return researcher.as_tool(tool_name="Researcher", tool_description=research_tool())


class Trader:
    def __init__(self, name: str, lastname="Trader", model_name="gpt-5.4-mini"):
        self.name = name
        self.lastname = lastname
        self.agent = None
        self.model_name = model_name
        self.do_trade = True
        self._budget_hooks = None
        self._last_usage = None

    async def create_agent(
        self, trader_mcp_servers, researcher_mcp_servers, decision_cutoff: datetime
    ) -> Agent:
        tool = await get_researcher_tool(researcher_mcp_servers, self.model_name, decision_cutoff)
        self.agent = Agent(
            name=self.name,
            instructions=trader_instructions(self.name, decision_cutoff),
            model=get_model(self.model_name),
            tools=[tool],
            mcp_servers=trader_mcp_servers,
            output_type=TradingDecision,
        )
        return self.agent

    async def get_account_report(self) -> str:
        account = await read_accounts_resource(self.name)
        account_json = json.loads(account)
        account_json.pop("portfolio_value_time_series", None)
        return json.dumps(account_json)

    async def run_agent(self, trader_mcp_servers, researcher_mcp_servers, budget: CycleBudget):
        decision_cutoff = datetime.now(timezone.utc)
        self.agent = await self.create_agent(
            trader_mcp_servers, researcher_mcp_servers, decision_cutoff
        )
        account = await self.get_account_report()
        strategy = await read_strategy_resource(self.name)
        message = (
            trade_message(self.name, strategy, account, decision_cutoff)
            if self.do_trade
            else rebalance_message(self.name, strategy, account, decision_cutoff)
        )
        self._budget_hooks = BudgetHooks(budget)
        result = await Runner.run(
            self.agent,
            message,
            max_turns=budget.max_turns,
            hooks=self._budget_hooks,
            run_config={"trace_include_sensitive_data": False},
        )
        usage = result.context_wrapper.usage
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
        output = result.final_output
        if isinstance(output, TradingDecision):
            output = output.model_copy(
                update={
                    "trader_prompt_version": TRADER_PROMPT_VERSION,
                    "research": output.research.model_copy(
                        update={"researcher_prompt_version": RESEARCHER_PROMPT_VERSION}
                    ),
                }
            )
        processed, error = pipeline.safely_process(self.name, output)
        if error:
            raise ValueError(error)
        return processed, usage

    async def run_with_mcp_servers(self, budget: CycleBudget):
        async with AsyncExitStack() as stack:
            trader_servers = [
                await stack.enter_async_context(server) for server in trader_mcp_servers()
            ]
            researcher_servers = [
                await stack.enter_async_context(server)
                for server in researcher_mcp_servers(self.name)
            ]
            return await self.run_agent(trader_servers, researcher_servers, budget)

    async def run_with_trace(self, context: CycleContext, budget: CycleBudget):
        trace_name = f"{self.name}-trading" if self.do_trade else f"{self.name}-rebalancing"
        trace_id = make_trace_id(f"{self.name.lower()}")
        metadata = {
            "cycle_id": context.cycle_id,
            "run_id": context.run_id or "live",
            "scenario_id": context.scenario_id or "live",
            "prompt_version": TRADER_PROMPT_VERSION,
            "market_mode": get_market_service().status().mode.value,
            "model": self.model_name,
            "decision_ids": [],
            "sensitive_payload_capture": False,
        }
        with trace(
            trace_name, trace_id=trace_id, group_id=context.run_id, metadata=metadata
        ) as current_trace:
            processed, usage = await self.run_with_mcp_servers(budget)
            decision_ids = [str(decision.decision_id) for _, decision, _ in processed]
            if hasattr(current_trace, "metadata"):
                current_trace.metadata["decision_ids"] = decision_ids
            return processed, usage, trace_id

    async def run(self, *, run_id: str | None = None):
        self._budget_hooks = None
        self._last_usage = None
        budget = CycleBudget.from_env()
        context = CycleContext.create(
            run_id=run_id or os.getenv("EVALUATION_RUN_ID"),
            scenario_id=os.getenv("EVALUATION_SCENARIO_ID"),
        )
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
                decision_ids=[str(decision.decision_id) for _, decision, _ in processed],
                trace_id=trace_id,
            )
        except asyncio.CancelledError:
            usage = self._last_usage or (
                self._budget_hooks.usage if self._budget_hooks is not None else None
            )
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
            usage = self._last_usage or (
                self._budget_hooks.usage if self._budget_hooks is not None else None
            )
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
