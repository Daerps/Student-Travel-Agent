# LangSmith 与 Redis 接入说明

本文先说明本项目里用到的 LangSmith、Redis 方法和使用方式，再说明它们在 Travel Agent 项目中的具体落点。

## 1. LangSmith 用了什么

LangSmith 在本项目中用于链路追踪，不参与业务决策，也不改变 Agent 输出。业务代码不直接操作 LangSmith SDK，而是统一调用 `utils/langsmith_tracing.py` 里的封装。

### 1.1 核心封装

文件：`utils/langsmith_tracing.py`

核心方法：

```python
start_trace(name, inputs=None, metadata=None, run_type="chain")
```

作用：

- 创建一个 LangSmith run。
- 如果当前已经有父 run，则创建 child run，形成嵌套链路。
- 如果 LangSmith 没开启或 API Key 不存在，则返回 no-op trace，不影响主流程。

返回对象是 `TraceRun`，主要方法：

```python
trace.end(outputs={...})
trace.end_error(error, outputs={...})
```

作用：

- `end()`：正常结束 trace，并上报 outputs、耗时。
- `end_error()`：异常结束 trace，并上报错误信息。

### 1.2 数据脱敏与截断

核心方法：

```python
sanitize_payload(value, max_chars=None, depth=0)
```

用途：

- 对 `api_key`、`token`、`secret`、`password`、`authorization` 等字段脱敏。
- 对过长文本截断，避免把完整 prompt、长回答、用户隐私全部上传。
- 对 list/dict 做深度限制，防止 trace payload 过大。

### 1.3 配置方式

文件：`config.py`

配置项：

```python
LANGSMITH_CONFIG = {
    "enabled": ...,
    "api_key": ...,
    "project": "travel-agent-dev",
    "endpoint": "https://api.smith.langchain.com",
    "max_payload_chars": 3000,
}
```

启用方式：

```powershell
$env:LANGSMITH_TRACING="true"
$env:LANGSMITH_API_KEY="你的 LangSmith Key"
$env:LANGSMITH_PROJECT="travel-agent-dev"
```

## 2. Redis 用了什么

Redis 在本项目中定位为缓存层和热数据层，不作为唯一持久化存储。长期记忆仍以 JSON 文件为主存储，Redis 丢失时系统可以回退到原有行为。

### 2.1 核心封装

文件：`utils/redis_client.py`

核心方法：

```python
get_redis_client()
```

作用：

- 根据 `REDIS_CONFIG` 创建 Redis client。
- 初始化时执行 `ping()` 检查可用性。
- Redis 未开启、未安装依赖、连接失败时返回 `None`，业务逻辑自动降级。

```python
key_for(*parts)
```

作用：

- 统一生成 Redis key。
- 自动加项目前缀。
- 对 user_id、session_id 等部分做 URL quote，避免特殊字符破坏 key。

```python
get_json(client, key, default=None)
set_json(client, key, value, ttl=None)
delete_keys(client, keys)
delete_pattern(client, pattern)
```

作用：

- 用 JSON 存取 Python dict/list/string。
- `set_json(..., ttl=...)` 对应 Redis `SETEX`。
- `delete_pattern()` 使用 `SCAN` 分批删除，避免生产中直接 `KEYS`。

### 2.2 Redis 数据结构

本项目当前用了三类 Redis 能力：

1. String / JSON value

用于偏好缓存和长期摘要缓存。

```text
GET
SET / SETEX
DEL
```

2. List

用于短期会话记忆。

```text
RPUSH
LTRIM
LRANGE
EXPIRE
```

3. TTL

用于控制短期记忆、摘要、偏好缓存生命周期。

```text
short_term_ttl = 3600
summary_ttl = 1800
preference_ttl = 86400
```

### 2.3 配置方式

文件：`config.py`

```python
REDIS_CONFIG = {
    "enabled": False,
    "url": "redis://localhost:6379/0",
    "key_prefix": "travel_agent",
    "short_term_ttl": 3600,
    "summary_ttl": 1800,
    "preference_ttl": 86400,
    "socket_timeout": 1.0,
}
```

启用方式：

```powershell
$env:REDIS_ENABLED="true"
$env:REDIS_URL="redis://localhost:6379/0"
$env:REDIS_KEY_PREFIX="travel_agent"
```

依赖：

```text
redis>=5.0.0
```

## 3. 本项目里的 LangSmith 实例

### 3.1 CLI 整体查询链路

文件：`cli.py`

主流程会创建一次 query trace，覆盖一次用户请求从输入到最终输出的链路。

记录内容包括：

- 用户输入。
- 意图识别阶段是否成功。
- 编排执行是否成功。
- 执行了多少个 Agent。
- 最终状态与耗时。

异常时调用：

```python
query_trace.end_error(e, outputs={"stage": "intention_agent"})
query_trace.end_error(e, outputs={"stage": "orchestration_agent"})
```

这能帮助定位失败发生在意图识别还是编排执行。

### 3.2 IntentionAgent 意图识别

文件：`agents/intention_agent.py`

这里使用 trace 记录：

- 输入消息数量。
- 可用 skill 描述。
- LLM 输出。
- JSON 解析结果。
- 识别出的 intents、key_entities、agent_schedule。

如果 LLM 输出不是合法 JSON，会通过 `end_error()` 记录解析失败。

### 3.3 OrchestrationAgent 编排与子 Agent 调用

文件：`agents/orchestration_agent.py`

这里主要有两类 trace：

1. 单个子 Agent 执行 trace

记录：

- agent_name。
- priority。
- reason。
- expected_output。
- previous_results 数量。
- 子 Agent 返回状态。

2. 最终回答 synthesis trace

记录：

- 多个 Agent 的结构化结果。
- 最终展示内容生成是否成功。

### 3.4 RAG 检索与生成

文件：`.claude/skills/ask-question/script/agent.py`

RAG 里有两个关键 trace：

```python
start_trace("rag.search_knowledge", ...)
start_trace("rag.generate_answer", ...)
```

`rag.search_knowledge` 记录：

- query。
- top_k。
- collection_name。
- retrieval_mode。
- vector_candidates。
- bm25_candidates。
- RRF 融合后的文档标题、source、distance、bm25_score、rrf_score。

`rag.generate_answer` 记录：

- 用户问题。
- 检索文档数量。
- 检索文档标题和来源。
- 生成答案预览。

## 4. 本项目里的 Redis 实例

### 4.1 Redis 客户端初始化

文件：`utils/redis_client.py`

流程：

```text
读取 REDIS_CONFIG
-> 如果 enabled=false，返回 None
-> import redis
-> Redis.from_url(...)
-> ping()
-> 成功后返回 client
```

设计重点：

- Redis 是可选增强。
- Redis 不可用时只打 warning，不中断主流程。
- 所有业务层都通过封装方法读写 Redis。

### 4.2 短期记忆：Redis List

文件：`context/short_term_memory.py`

key 形式：

```text
travel_agent:short_term:{user_id}:{session_id}
```

写入时：

```text
RPUSH message_json
LTRIM key -max_messages -1
EXPIRE key short_term_ttl
```

读取时：

```text
LRANGE key -(n_turns * 2) -1
```

作用：

- 原来短期记忆只在 Python 进程内的 `self.messages`。
- 接入 Redis 后，同一个 session 的最近对话可以跨对象实例读取。
- 本地 `self.messages` 仍保留，用于 Redis 不可用时 fallback。

### 4.3 用户偏好：Redis JSON 缓存

文件：`context/long_term_memory.py`

key 形式：

```text
travel_agent:preferences:{user_id}
```

读取逻辑：

```text
先 GET Redis
命中 -> 返回偏好 dict
未命中 -> 从 JSON 主存储构造偏好 dict -> SETEX 写回 Redis
```

写入逻辑：

```text
save_preference()
-> 先更新 JSON 文件
-> 再刷新 Redis preference cache
```

设计原因：

- JSON 文件仍是长期记忆主存储。
- Redis 只是减少频繁读取和格式转换。
- Redis 丢失不会导致用户偏好丢失。

### 4.4 长期记忆摘要：Redis String 缓存

文件：`context/memory_manager.py`

key 形式：

```text
travel_agent:summary:{user_id}:{max_messages}:{history_hash}
```

`history_hash` 来源：

```text
历史聊天记录
历史行程记录
max_messages
```

读取逻辑：

```text
构造 history_hash
-> 查 Redis summary
-> 命中直接返回
-> 未命中调用 LLM 总结
-> SETEX 写 Redis
```

好处：

- 避免每次意图识别前重复调用 LLM 总结长期历史。
- 聊天历史或行程历史变化后，hash 改变，自动生成新的 cache key。
- 旧摘要会按 TTL 过期，不需要强制全量扫描删除。

### 4.5 MemoryManager 如何串起来

文件：`context/memory_manager.py`

初始化时：

```python
self.redis_config = get_redis_config()
self.redis_client = get_redis_client()

self.short_term = ShortTermMemory(
    max_turns=10,
    user_id=user_id,
    session_id=session_id,
    redis_client=self.redis_client,
    redis_ttl=self.redis_config.get("short_term_ttl", 3600),
)

self.long_term = LongTermMemory(
    user_id,
    storage_path,
    redis_client=self.redis_client,
    redis_ttl=self.redis_config.get("preference_ttl", 86400),
)
```

也就是说，CLI、OrchestrationAgent、Skill Agent 仍然通过原来的 `memory_manager.short_term` 和 `memory_manager.long_term` 使用记忆，不需要直接知道 Redis 存在。

## 5. 面试回答模板

可以这样说：

```text
LangSmith 主要用于可观测性，我没有把 tracing 逻辑散落在业务代码里，而是封装了 start_trace、end、end_error 和 sanitize_payload。一次用户请求会从 CLI 形成父 trace，意图识别、子 Agent 执行、RAG 检索和答案生成会形成子 trace，这样可以看到每一步输入、输出、耗时和异常阶段。

Redis 在项目中作为缓存层和热数据层，不替代长期存储。短期记忆用 Redis List 保存当前 session 最近 N 轮对话，通过 RPUSH、LTRIM、LRANGE 和 EXPIRE 管理；用户偏好用 Redis JSON 缓存做 read-through/write-through，JSON 文件仍是主存储；长期历史摘要用 user_id、max_messages 和历史内容 hash 作为 key 缓存，避免每次都调用 LLM 总结。Redis 不可用时系统自动回退到本地内存和 JSON 存储。
```

## 6. 当前边界

当前 Redis 接入覆盖：

- 短期记忆。
- 用户偏好缓存。
- 长期摘要缓存。

当前没有覆盖：

- Milvus 向量检索结果缓存。
- RAG BM25 索引持久化到 Redis。
- Agent 实例缓存。
- 分布式锁或队列。

这些可以作为后续扩展，但不是当前版本的事实。
