from contextlib import AsyncExitStack
from .accounts_client import read_accounts_resource, read_strategy_resource
from .tracers import make_trace_id
from agents import Agent, Tool, Runner, OpenAIChatCompletionsModel, trace
from openai import AsyncOpenAI
from dotenv import load_dotenv
import os
import json
from datetime import datetime, timezone
from functools import lru_cache
from .templates import (
    researcher_instructions,
    trader_instructions,
    trade_message,
    rebalance_message,
    research_tool,
)
from .mcp_servers import trader_mcp_servers, researcher_mcp_servers
from .market import get_market_observation
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
from .research import RESEARCHER_PROMPT_VERSION, TRADER_PROMPT_VERSION

load_dotenv(override=True)

deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
google_api_key = os.getenv("GOOGLE_API_KEY")
grok_api_key = os.getenv("GROK_API_KEY")
openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
GROK_BASE_URL = "https://api.x.ai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

MAX_TURNS = 30

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
        return OpenAIChatCompletionsModel(model=model_name, openai_client=_optional_client("openrouter"))
    elif "deepseek" in model_name:
        return OpenAIChatCompletionsModel(model=model_name, openai_client=_optional_client("deepseek"))
    elif "grok" in model_name:
        return OpenAIChatCompletionsModel(model=model_name, openai_client=_optional_client("grok"))
    elif "gemini" in model_name:
        return OpenAIChatCompletionsModel(model=model_name, openai_client=_optional_client("gemini"))
    else:
        return model_name


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

    async def create_agent(self, trader_mcp_servers, researcher_mcp_servers, decision_cutoff: datetime) -> Agent:
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

    async def run_agent(self, trader_mcp_servers, researcher_mcp_servers):
        decision_cutoff = datetime.now(timezone.utc)
        self.agent = await self.create_agent(trader_mcp_servers, researcher_mcp_servers, decision_cutoff)
        account = await self.get_account_report()
        strategy = await read_strategy_resource(self.name)
        message = (
            trade_message(self.name, strategy, account, decision_cutoff)
            if self.do_trade
            else rebalance_message(self.name, strategy, account, decision_cutoff)
        )
        result = await Runner.run(self.agent, message, max_turns=MAX_TURNS)
        repository = DecisionRepository()
        proposal_service = ProposalService(repository, get_market_observation)
        risk_service = RiskService(repository, RiskEngine(RiskPolicy.from_env()), get_market_observation)
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
        return processed

    async def run_with_mcp_servers(self):
        async with AsyncExitStack() as stack:
            trader_servers = [
                await stack.enter_async_context(server) for server in trader_mcp_servers()
            ]
            researcher_servers = [
                await stack.enter_async_context(server)
                for server in researcher_mcp_servers(self.name)
            ]
            await self.run_agent(trader_servers, researcher_servers)

    async def run_with_trace(self):
        trace_name = f"{self.name}-trading" if self.do_trade else f"{self.name}-rebalancing"
        trace_id = make_trace_id(f"{self.name.lower()}")
        with trace(trace_name, trace_id=trace_id):
            await self.run_with_mcp_servers()

    async def run(self):
        try:
            await self.run_with_trace()
        except Exception as e:
            print(f"Error running trader {self.name}: {e}")
        self.do_trade = not self.do_trade
