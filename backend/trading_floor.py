import asyncio
import os
import re
import signal
from datetime import datetime, timedelta, timezone
from typing import List

from agents import add_trace_processor
from dotenv import load_dotenv

import backend.startup as startup

from .agent_runs import AgentRunConflict, AgentRunRepository, UnchangedMarketData
from .config import validate_startup
from .decisions.repository import DecisionRepository
from .market import MarketDataError, get_market_observation, is_market_open
from .observability import TelemetryRepository, safe_error
from .strategies import ensure_default_strategies
from .tracers import LogTracer
from .traders import Trader

load_dotenv()

RUN_EVERY_N_MINUTES = startup.runtime_settings.scheduler_interval_minutes
SCHEDULER_MODE = startup.runtime_settings.scheduler_mode
SCHEDULER_DAILY_TIME_UTC = startup.runtime_settings.scheduler_daily_time_utc
RUN_EVEN_WHEN_MARKET_IS_CLOSED = (
    os.getenv("RUN_EVEN_WHEN_MARKET_IS_CLOSED", "false").strip().lower() == "true"
)
USE_MANY_MODELS = os.getenv("USE_MANY_MODELS", "false").strip().lower() == "true"
DEFAULT_MODEL_NAME = "gpt-5.4-mini"
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
_trace_processor_registered = False


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
    ensure_default_strategies()
    traders = []
    for name, lastname, model_name in zip(names, lastnames, model_names):
        traders.append(Trader(name, lastname, model_name))
    return traders


def ensure_trace_processor() -> None:
    global _trace_processor_registered
    if not _trace_processor_registered:
        add_trace_processor(LogTracer())
        _trace_processor_registered = True


async def _run_cycle(
    traders: list[Trader], max_concurrency: int = 1, *, run_id: str | None = None
) -> None:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_one(trader: Trader) -> None:
        async with semaphore:
            if run_id is None:
                await trader.run()
            else:
                await trader.run(run_id=run_id)

    await asyncio.gather(*(run_one(trader) for trader in traders))


def seconds_until_daily_run(now: datetime, configured_time: str) -> float:
    """Return a positive delay to the next configured UTC wall-clock time."""
    hour, minute = (int(part) for part in configured_time.split(":"))
    target = now.astimezone(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def is_daily_run_day(now: datetime) -> bool:
    """Run post-close cycles on UTC weekdays; provider freshness remains authoritative."""
    return now.astimezone(timezone.utc).weekday() < 5


async def reserve_agent_run(
    repository: AgentRunRepository,
    *,
    trigger: str,
    requested_by: str,
    idempotency_key: str | None = None,
):
    """Probe a typed snapshot before atomically reserving it for one run."""
    observation = await asyncio.to_thread(get_market_observation, "SPY")
    if observation.is_stale:
        raise MarketDataError(
            f"{observation.mode.value} market snapshot is stale at "
            f"{observation.market_timestamp.isoformat()}"
        )
    key = idempotency_key or (
        f"scheduled:{observation.mode.value}:{observation.market_timestamp.isoformat()}"
    )
    return repository.request(
        trigger=trigger,
        requested_by=requested_by,
        idempotency_key=key,
        observation=observation,
    )


async def execute_agent_run(
    run_id: str,
    *,
    repository: AgentRunRepository | None = None,
    traders: list[Trader] | None = None,
    max_concurrency: int | None = None,
):
    """Execute a reserved run and derive its durable outcome from all trader cycles."""
    repository = repository or AgentRunRepository()
    traders = traders or create_traders()
    concurrency = max_concurrency or startup.runtime_settings.agent_max_concurrency
    ensure_trace_processor()
    repository.mark_running(run_id)
    try:
        await _run_cycle(traders, max_concurrency=concurrency, run_id=run_id)
        status, error = repository.cycle_outcome(run_id, len(traders))
        record = repository.finish(run_id, status, error)
        snapshots = DecisionRepository(repository.path)
        for trader in traders:
            try:
                snapshots.record_portfolio_snapshot(
                    trader.name, record.completed_at or datetime.now(timezone.utc)
                )
            except Exception as exc:
                print(f"Portfolio snapshot failed for {trader.name}: {safe_error(exc)}")
        return record
    except asyncio.CancelledError:
        repository.finish(run_id, "interrupted", "process shutdown interrupted agent run")
        raise
    except Exception as exc:
        return repository.finish(run_id, "failed", exc)


async def run_every_n_minutes(
    stop_event: asyncio.Event | None = None,
    traders: list[Trader] | None = None,
    *,
    interval_seconds: float | None = None,
    shutdown_grace_seconds: float | None = None,
):
    """Run cycles until stopped, allowing bounded completion of in-flight work."""
    runtime = validate_startup("scheduler")
    ensure_trace_processor()
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
    daily_schedule = runtime.scheduler_mode == "daily_utc" and interval_seconds is None
    while not stop_event.is_set():
        if daily_schedule:
            delay = seconds_until_daily_run(
                datetime.now(timezone.utc), runtime.scheduler_daily_time_utc
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
        scheduled_now = datetime.now(timezone.utc)
        should_run = (
            is_daily_run_day(scheduled_now)
            if daily_schedule
            else RUN_EVEN_WHEN_MARKET_IS_CLOSED or is_market_open()
        )
        if should_run:
            if interval_seconds is not None:
                cycle_task = asyncio.create_task(
                    _run_cycle(traders, max_concurrency=runtime.agent_max_concurrency)
                )
            else:
                repository = AgentRunRepository()
                run = None
                created = False
                try:
                    run, created = await reserve_agent_run(
                        repository,
                        trigger="scheduled",
                        requested_by="scheduler",
                    )
                except (AgentRunConflict, UnchangedMarketData, MarketDataError) as exc:
                    print(f"Skipping run: {exc}")
                    cycle_task = None
                else:
                    cycle_task = (
                        asyncio.create_task(
                            execute_agent_run(
                                run.run_id,
                                repository=repository,
                                traders=traders,
                                max_concurrency=runtime.agent_max_concurrency,
                            )
                        )
                        if created
                        else None
                    )
                if run is not None and not created:
                    print(f"Skipping run: scheduled request {run.run_id} already exists")
            if cycle_task is None:
                if not daily_schedule:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    except TimeoutError:
                        pass
                continue
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
            reason = "non-scheduled day" if daily_schedule else "market is closed"
            print(f"Skipping run: {reason}")
        if not daily_schedule:
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
    schedule = (
        f"daily at {SCHEDULER_DAILY_TIME_UTC} UTC"
        if SCHEDULER_MODE == "daily_utc"
        else f"every {RUN_EVERY_N_MINUTES} minutes"
    )
    print(f"Starting scheduler {schedule}")
    asyncio.run(scheduler_main())
