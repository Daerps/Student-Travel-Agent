"""
Optional LangSmith tracing helpers.

Business code calls start_trace(...); this module decides whether tracing is
enabled and safely degrades to no-op when LangSmith is not configured.
"""
from __future__ import annotations

import contextvars
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_current_run = contextvars.ContextVar("langsmith_current_run", default=None)
_SENSITIVE_KEYWORDS = ("api_key", "apikey", "token", "secret", "password", "authorization")


def _get_config() -> Dict[str, Any]:
    try:
        from config import LANGSMITH_CONFIG

        return LANGSMITH_CONFIG
    except Exception:
        return {"enabled": False}


def _is_enabled() -> bool:
    cfg = _get_config()
    return bool(cfg.get("enabled") and cfg.get("api_key"))


def _configure_env() -> Dict[str, Any]:
    cfg = _get_config()
    api_key = cfg.get("api_key", "")
    project = cfg.get("project", "travel-agent-dev")
    endpoint = cfg.get("endpoint", "https://api.smith.langchain.com")

    # LangSmith accepts LANGSMITH_*; older integrations may still inspect
    # LANGCHAIN_* names, so set both without overwriting user-provided values.
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", project)
    os.environ.setdefault("LANGSMITH_ENDPOINT", endpoint)
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", api_key)
    os.environ.setdefault("LANGCHAIN_PROJECT", project)
    os.environ.setdefault("LANGCHAIN_ENDPOINT", endpoint)
    return cfg


def _max_chars() -> int:
    return int(_get_config().get("max_payload_chars", 10000))


def sanitize_payload(value: Any, max_chars: Optional[int] = None, depth: int = 0) -> Any:
    """Trim large payloads and mask obvious secrets before sending traces."""
    if max_chars is None:
        max_chars = _max_chars()

    if depth > 4:
        return "<max-depth>"

    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_str = str(key)
            if any(token in key_str.lower() for token in _SENSITIVE_KEYWORDS):
                sanitized[key_str] = "<redacted>"
            else:
                sanitized[key_str] = sanitize_payload(item, max_chars=max_chars, depth=depth + 1)
        return sanitized

    if isinstance(value, (list, tuple)):
        limited = list(value[:20])
        if len(value) > 20:
            limited.append(f"<truncated {len(value) - 20} items>")
        return [sanitize_payload(item, max_chars=max_chars, depth=depth + 1) for item in limited]

    if isinstance(value, str):
        if len(value) > max_chars:
            return value[:max_chars] + f"... <truncated {len(value) - max_chars} chars>"
        return value

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    return sanitize_payload(str(value), max_chars=max_chars, depth=depth + 1)


class TraceRun:
    """Small wrapper around a LangSmith RunTree or a no-op trace."""

    def __init__(self, run=None, token=None, enabled: bool = False):
        self.run = run
        self.token = token
        self.enabled = enabled
        self.started_at = time.perf_counter()
        self.ended = False

    def end(self, outputs: Optional[Dict[str, Any]] = None, error: Optional[BaseException] = None) -> None:
        if self.ended:
            return
        self.ended = True

        if not self.enabled or self.run is None:
            return

        safe_outputs = sanitize_payload(outputs or {})
        safe_outputs.setdefault("latency_sec", round(time.perf_counter() - self.started_at, 3))

        try:
            if error is not None:
                self.run.end(outputs=safe_outputs, error=repr(error))
            else:
                self.run.end(outputs=safe_outputs)
            self.run.patch()
        except Exception as exc:
            logger.debug("LangSmith trace end failed: %s", exc)
        finally:
            if self.token is not None:
                try:
                    _current_run.reset(self.token)
                except Exception:
                    pass

    def end_error(self, error: BaseException, outputs: Optional[Dict[str, Any]] = None) -> None:
        self.end(outputs=outputs, error=error)


def start_trace(
    name: str,
    inputs: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    run_type: str = "chain",
) -> TraceRun:
    """Start a LangSmith run, nested under the current run when present."""
    if not _is_enabled():
        return TraceRun()

    try:
        cfg = _configure_env()
        from langsmith.run_trees import RunTree

        parent = _current_run.get()
        safe_inputs = sanitize_payload(inputs or {})
        safe_metadata = sanitize_payload(metadata or {})

        if parent is not None:
            run = parent.create_child(
                name=name,
                run_type=run_type,
                inputs=safe_inputs,
                extra={"metadata": safe_metadata},
            )
        else:
            run = RunTree(
                name=name,
                run_type=run_type,
                inputs=safe_inputs,
                extra={"metadata": safe_metadata},
                project_name=cfg.get("project", "travel-agent-dev"),
            )

        run.post()
        token = _current_run.set(run)
        return TraceRun(run=run, token=token, enabled=True)
    except Exception as exc:
        logger.debug("LangSmith trace start failed: %s", exc)
        return TraceRun()
