from .traders import Trader
from typing import List
import asyncio
from .tracers import LogTracer
from agents import add_trace_processor
from .market import is_market_open
from dotenv import load_dotenv
import os
import re

load_dotenv(override=True)

RUN_EVERY_N_MINUTES = int(os.getenv("RUN_EVERY_N_MINUTES", "60"))
RUN_EVEN_WHEN_MARKET_IS_CLOSED = (
    os.getenv("RUN_EVEN_WHEN_MARKET_IS_CLOSED", "false").strip().lower() == "true"
)
USE_MANY_MODELS = os.getenv("USE_MANY_MODELS", "false").strip().lower() == "true"
DEFAULT_MODEL_NAME = "gpt-5.4-mini"
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")


def configured_model_name(value: str | None) -> str:
    """Validate a configured API model identifier without rewriting it."""
    model_name = (DEFAULT_MODEL_NAME if value is None else value).strip()
    if not _MODEL_NAME_PATTERN.fullmatch(model_name):
        raise ValueError(
            "MODEL_NAME must be a non-empty model identifier containing only letters, "
            "numbers, '.', '_', ':', '/', or '-'"
        )
    return model_name


MODEL_NAME = configured_model_name(os.getenv("MODEL_NAME"))

names = ["Warren", "George", "Ray", "Cathie"]
lastnames = ["Patience", "Bold", "Systematic", "Crypto"]

if USE_MANY_MODELS:
    model_names = [
        "gpt-5.5",
        "deepseek-v4-flash",
        "gemini-3.5-flash",
        "grok-4.3",
    ]
    short_model_names = ["GPT 5.5", "DeepSeek V4", "Gemini 3.5 Flash", "Grok 4.3"]
else:
    model_names = [MODEL_NAME] * 4
    short_model_names = [MODEL_NAME] * 4


def create_traders() -> List[Trader]:
    traders = []
    for name, lastname, model_name in zip(names, lastnames, model_names):
        traders.append(Trader(name, lastname, model_name))
    return traders


async def run_every_n_minutes():
    add_trace_processor(LogTracer())
    traders = create_traders()
    while True:
        if RUN_EVEN_WHEN_MARKET_IS_CLOSED or is_market_open():
            await asyncio.gather(*[trader.run() for trader in traders])
        else:
            print("Market is closed, skipping run")
        await asyncio.sleep(RUN_EVERY_N_MINUTES * 60)


if __name__ == "__main__":
    print(f"Starting scheduler to run every {RUN_EVERY_N_MINUTES} minutes")
    asyncio.run(run_every_n_minutes())
