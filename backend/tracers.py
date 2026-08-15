from threading import Lock

from agents import Span, Trace, TracingProcessor

from .database import write_log
from .observability import safe_error


class LogTracer(TracingProcessor):
    """Mirror SDK trace events to each agent's local activity log."""

    def __init__(self) -> None:
        self._agent_names: dict[str, str] = {}
        self._lock = Lock()

    def _get_name(self, trace_or_span: Trace | Span) -> str | None:
        with self._lock:
            return self._agent_names.get(trace_or_span.trace_id)

    def on_trace_start(self, trace) -> None:
        metadata = getattr(trace, "metadata", None)
        name = metadata.get("agent_name") if isinstance(metadata, dict) else None
        if name:
            with self._lock:
                self._agent_names[trace.trace_id] = str(name)
            write_log(name, "trace", f"Started: {trace.name}")

    def on_trace_end(self, trace) -> None:
        name = self._get_name(trace)
        if name:
            write_log(name, "trace", f"Ended: {trace.name}")
        with self._lock:
            self._agent_names.pop(trace.trace_id, None)

    def on_span_start(self, span) -> None:
        name = self._get_name(span)
        type = span.span_data.type if span.span_data else "span"
        if name:
            message = "Started"
            if span.span_data:
                if span.span_data.type:
                    message += f" {span.span_data.type}"
                if hasattr(span.span_data, "name") and span.span_data.name:
                    message += f" {span.span_data.name}"
                if hasattr(span.span_data, "server") and span.span_data.server:
                    message += f" {span.span_data.server}"
            if span.error:
                message += f" {safe_error(span.error)}"
            write_log(name, type, message)

    def on_span_end(self, span) -> None:
        name = self._get_name(span)
        type = span.span_data.type if span.span_data else "span"
        if name:
            message = "Ended"
            if span.span_data:
                if span.span_data.type:
                    message += f" {span.span_data.type}"
                if hasattr(span.span_data, "name") and span.span_data.name:
                    message += f" {span.span_data.name}"
                if hasattr(span.span_data, "server") and span.span_data.server:
                    message += f" {span.span_data.server}"
            if span.error:
                message += f" {safe_error(span.error)}"
            write_log(name, type, message)

    def force_flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass
