"""
Configuration for the  Multi-Agent System
"""
import json
import os
from pathlib import Path


def _load_local_secrets() -> dict:
    """Load local secrets ignored by git. Environment variables still take priority."""
    secrets_path = Path(__file__).with_name("local_secrets.json")
    if not secrets_path.exists():
        return {}
    try:
        with open(secrets_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_LOCAL_SECRETS = _load_local_secrets()

# LLM Configuration
LLM_CONFIG = {
    "api_key": os.getenv("LLM_API_KEY", _LOCAL_SECRETS.get("llm_api_key", "")),
    "model_name": "deepseek-v4-pro",
    "base_url": "https://api.deepseek.com",
    "temperature": 0.7,
    "max_tokens": 8192,
}

# System Configuration
SYSTEM_CONFIG = {
    "enable_llm": True,  # Set to True to use LLM (recommended), False for rule-based
    "log_level": "INFO",
    "max_retries": 3,
    "timeout": 60,  # Increased timeout for better stability
}

REDIS_CONFIG = {
    "enabled": str(os.getenv("REDIS_ENABLED", _LOCAL_SECRETS.get("redis_enabled", "false"))).lower() == "true",
    "url": os.getenv("REDIS_URL", _LOCAL_SECRETS.get("redis_url", "redis://localhost:6379/0")),
    "key_prefix": os.getenv("REDIS_KEY_PREFIX", _LOCAL_SECRETS.get("redis_key_prefix", "travel_agent")),
    "short_term_ttl": int(os.getenv("REDIS_SHORT_TERM_TTL", _LOCAL_SECRETS.get("redis_short_term_ttl", 3600))),
    "summary_ttl": int(os.getenv("REDIS_SUMMARY_TTL", _LOCAL_SECRETS.get("redis_summary_ttl", 1800))),
    "preference_ttl": int(os.getenv("REDIS_PREFERENCE_TTL", _LOCAL_SECRETS.get("redis_preference_ttl", 86400))),
    "socket_timeout": float(os.getenv("REDIS_SOCKET_TIMEOUT", _LOCAL_SECRETS.get("redis_socket_timeout", 1.0))),
}

# RAG 知识库：嵌入模型（本地路径，无需连 HuggingFace）
RAG_CONFIG = {
    "embedding_model": "data/models/bge-small-zh-v1.5",
}

# 连接与可用性：重试、熔断、健康检查
RESILIENCE_CONFIG = {
    "max_retries": 3,              # 单次请求最大重试次数（与 SYSTEM_CONFIG 对齐）
    "retry_base_delay_sec": 1.0,   # 重试退避基数（秒）
    "retry_max_delay_sec": 30.0,   # 重试退避上限（秒）
    "circuit_failure_threshold": 5, # 连续失败多少次后熔断
    "circuit_recovery_timeout_sec": 60.0,  # 熔断后多少秒进入半开
    "circuit_half_open_successes": 2,      # 半开状态下连续成功多少次后关闭
    "health_check_timeout_sec": 10.0,      # 健康检查请求超时（秒）
}

# LangSmith tracing：默认关闭；设置 LANGSMITH_TRACING=true 且配置 LANGSMITH_API_KEY 后启用。
LANGSMITH_CONFIG = {
    "enabled": os.getenv("LANGSMITH_TRACING", "true").lower() == "true",
    "api_key": os.getenv("LANGSMITH_API_KEY", _LOCAL_SECRETS.get("langsmith_api_key", "")),
    "project": os.getenv("LANGSMITH_PROJECT", "travel-agent-dev"),
    "endpoint": os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
    "max_payload_chars": int(os.getenv("LANGSMITH_MAX_PAYLOAD_CHARS", "3000")),
}
