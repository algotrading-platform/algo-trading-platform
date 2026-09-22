# ============================================================
# core/telemetry.py
#
# Enterprise Phase 1 observability -- Application Insights via the
# Azure Monitor OpenTelemetry distro, wired to the SAME Log Analytics
# workspace (algo-law-rjw4desia2hqk) already backing the Container
# Apps Environment. Before this, every one of "scanning issues",
# "orders placed late", "sandbox 401s", and "timeframe overlap" had
# to be diagnosed after the fact by reading raw container logs -- this
# module gives each of those a real, queryable metric.
#
# Safe by construction: if APPLICATIONINSIGHTS_CONNECTION_STRING isn't
# set (local dev, or a Container App that hasn't picked up the secret
# yet), every function below becomes a cheap no-op. Nothing that calls
# into this module needs its own try/except -- init failures and a
# missing connection string are handled here, once.
# ============================================================

import logging
import os
import threading

log = logging.getLogger("telemetry")

_lock = threading.Lock()
_configured = False
_meter = None

_scan_duration_hist = None
_scan_coverage_hist = None
_signal_latency_hist = None
_sandbox_result_counter = None


def init_telemetry(role_name: str) -> None:
    """
    Call once, at process startup (each of run_ws_listener.py,
    run_single_scan.py, and app/dashboard/dashboard.py). `role_name`
    tags every emitted signal (e.g. "ws-listener", "scanner",
    "dashboard") so Application Insights can separate them even though
    they share one App Insights resource.
    """
    global _configured, _meter
    with _lock:
        if _configured:
            return
        _configured = True  # only ever try once per process, success or not

        conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
        if not conn_str:
            log.info(f"{role_name}: APPLICATIONINSIGHTS_CONNECTION_STRING not set — telemetry disabled")
            return

        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            from opentelemetry import metrics

            configure_azure_monitor(connection_string=conn_str)
            _meter = metrics.get_meter("algo_trading", version="1.0")
            os.environ.setdefault("OTEL_SERVICE_NAME", role_name)

            # The Azure SDK's HTTP logging policy logs every export's full
            # request/response (headers included) at INFO level -- with a
            # frequent default export interval, this drowns out the actual
            # application's own INFO logs (ws_listener's connect/reconnect
            # lines, scan start/done, etc.) in the container's log stream,
            # defeating the whole point of adding observability. Quieting
            # just the transport-noise loggers, not azure.monitor's own
            # "telemetry configured"-type messages.
            for noisy in (
                "azure.core.pipeline.policies.http_logging_policy",
                "azure.monitor.opentelemetry.exporter.export._base",
            ):
                logging.getLogger(noisy).setLevel(logging.WARNING)

            log.info(f"{role_name}: Application Insights telemetry configured")
        except Exception as e:
            # Never let a telemetry misconfiguration take down the actual
            # trading process -- worst case, metrics just don't show up.
            log.warning(f"{role_name}: telemetry init failed, continuing without it: {e}")
            _meter = None


def _ensure_instruments() -> bool:
    """Lazily creates the metric instruments on first real use, once
    _meter exists. Returns False (and every record_* below no-ops) if
    telemetry was never configured."""
    global _scan_duration_hist, _scan_coverage_hist, _signal_latency_hist, _sandbox_result_counter
    if _meter is None:
        return False
    if _scan_duration_hist is not None:
        return True
    with _lock:
        if _scan_duration_hist is not None:
            return True
        _scan_duration_hist = _meter.create_histogram(
            "algo_scan_duration_seconds", unit="s",
            description="Wall-clock duration of one scan cycle, by timeframe",
        )
        _scan_coverage_hist = _meter.create_histogram(
            "algo_scan_coverage_pct", unit="%",
            description="Percent of the instrument universe completed before the scan's 300s deadline",
        )
        _signal_latency_hist = _meter.create_histogram(
            "algo_signal_to_order_latency_seconds", unit="s",
            description="Time from signal detection (trade_intents enqueue) to order placement completing",
        )
        _sandbox_result_counter = _meter.create_counter(
            "algo_sandbox_order_results",
            description="Count of sandbox place_order attempts, by outcome",
        )
    return True


def record_scan_duration(strategy_name: str, tf_name: str, duration_seconds: float, completed: int, total: int) -> None:
    """Called once per scan cycle (run_scan/run_multi_scan), right
    after the 'SCAN DONE'/'MULTI SCAN DONE' log line -- same numbers,
    just also emitted as a metric so a 300s-deadline partial scan
    shows up on a dashboard instead of only in grepped logs."""
    if not _ensure_instruments():
        return
    attrs = {"strategy": strategy_name, "timeframe": tf_name}
    try:
        _scan_duration_hist.record(duration_seconds, attrs)
        if total:
            _scan_coverage_hist.record(round(completed / total * 100, 1), attrs)
    except Exception as e:
        log.debug(f"record_scan_duration failed (non-fatal): {e}")


def record_signal_to_order_latency(seconds: float, strategy: str, source_label: str) -> None:
    """Called from ws_listener.py's order-worker right after an intent
    finishes executing -- `seconds` is now() - the trade_intents row's
    created_at, i.e. exactly the "how late was this order" number the
    outbox pattern was built to make visible."""
    if not _ensure_instruments():
        return
    try:
        _signal_latency_hist.record(seconds, {"strategy": strategy, "source": source_label})
    except Exception as e:
        log.debug(f"record_signal_to_order_latency failed (non-fatal): {e}")


def record_sandbox_result(success: bool) -> None:
    """Called from SandboxClient.place_order() for every attempt (not
    just failures), so the 401 rate is a ratio you can chart, not a
    count of log lines someone has to go find."""
    if not _ensure_instruments():
        return
    try:
        _sandbox_result_counter.add(1, {"outcome": "success" if success else "failure"})
    except Exception as e:
        log.debug(f"record_sandbox_result failed (non-fatal): {e}")
