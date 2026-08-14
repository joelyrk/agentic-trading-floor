import asyncio
import os
import re
import signal
from typing import List

from agents import add_trace_processor
from dotenv import load_dotenv

import backend.startup as startup

from .config import validate_startup
from .market import is_market_open
from .observability import TelemetryRepository
from .tracers import LogTracer
from .traders import Trader

load_dotenv()

RUN_EVERY_N_MINUTES = startup.runtime_settings.scheduler_interval_minutes
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


async def _run_cycle(traders: list[Trader]) -> None:
    await asyncio.gather(*(trader.run() for trader in traders))


async def run_every_n_minutes(
    stop_event: asyncio.Event | None = None,
    traders: list[Trader] | None = None,
    *,
    interval_seconds: float | None = None,
    shutdown_grace_seconds: float | None = None,
):
    """Run cycles until stopped, allowing bounded completion of in-flight work."""
    runtime = validate_startup("scheduler")
    add_trace_processor(LogTracer())
    traders = traders or create_traders()
    stop_event = stop_event or asyncio.Event()
    interval = (
        interval_seconds
        if interval_seconds is not None
        else runtime.scheduler_interval_minutes * 60
    )
    grace = (
        shutdown_grace_seconds
        if shutdown_grace_seconds is not None
        else runtime.shutdown_grace_seconds
    )
    TelemetryRepository().recover_interrupted_cycles()
    while not stop_event.is_set():
        if RUN_EVEN_WHEN_MARKET_IS_CLOSED or is_market_open():
            cycle_task = asyncio.create_task(_run_cycle(traders))
            stop_task = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait(
                {cycle_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done and not cycle_task.done():
                try:
                    await asyncio.wait_for(cycle_task, timeout=grace)
                except TimeoutError:
                    cycle_task.cancel()
                    await asyncio.gather(cycle_task, return_exceptions=True)
                return
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await cycle_task
        else:
            print("Market is closed, skipping run")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass


async def scheduler_main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows event loops
            pass
    await run_every_n_minutes(stop_event)


if __name__ == "__main__":
    print(f"Starting scheduler to run every {RUN_EVERY_N_MINUTES} minutes")
    asyncio.run(scheduler_main())
