from types import SimpleNamespace
from unittest.mock import Mock

from agents import gen_trace_id

from backend.tracers import LogTracer


def test_sdk_trace_ids_use_platform_format() -> None:
    trace_id = gen_trace_id()

    assert trace_id.startswith("trace_")
    assert len(trace_id) == 38
    assert all(character in "0123456789abcdef" for character in trace_id.removeprefix("trace_"))


def test_log_tracer_uses_agent_metadata_for_trace_and_spans(monkeypatch) -> None:
    write_log = Mock()
    monkeypatch.setattr("backend.tracers.write_log", write_log)
    processor = LogTracer()
    trace = SimpleNamespace(
        trace_id="trace_0123456789abcdef0123456789abcdef",
        name="Cathie-trading",
        metadata={"agent_name": "cathie"},
    )
    span = SimpleNamespace(
        trace_id=trace.trace_id,
        span_data=SimpleNamespace(type="generation", name="recommendation"),
        error=None,
    )

    processor.on_trace_start(trace)
    processor.on_span_start(span)
    processor.on_span_end(span)
    processor.on_trace_end(trace)

    assert write_log.call_args_list[0].args == ("cathie", "trace", "Started: Cathie-trading")
    assert write_log.call_args_list[-1].args == ("cathie", "trace", "Ended: Cathie-trading")
    assert len(write_log.call_args_list) == 4


def test_log_tracer_ignores_unattributed_traces(monkeypatch) -> None:
    write_log = Mock()
    monkeypatch.setattr("backend.tracers.write_log", write_log)
    processor = LogTracer()
    trace = SimpleNamespace(
        trace_id="trace_0123456789abcdef0123456789abcdef",
        name="unattributed",
        metadata={},
    )

    processor.on_trace_start(trace)
    processor.on_trace_end(trace)

    write_log.assert_not_called()
