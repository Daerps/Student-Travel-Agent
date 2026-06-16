# init_knowledge_base.py 代码分析文档

> 文件路径: `.claude/skills/ask-question/script/init_knowledge_base.py`

## 1. 功能概述

该脚本用于**初始化 RAG (Retrieval-Augmented Generation) 知识库**。核心流程是：

1. 从 `data/documents` 目录加载所有 `.txt` 格式的商旅相关文档
2. 对文档进行**文本切分 (Chunking)**，生成适合向量化的文本片段
3. 通过 `RAGKnowledgeAgent` 将切分后的文档**嵌入并存入 Milvus 向量数据库**
4. 完成后进行**检索测试**，验证知识库可用性

## 2. 代码流程图

```
main()
  │
  ├─ 1. 创建 OpenAIChatModel (LLM 模型)
  │
  ├─ 2. 初始化 RAGKnowledgeAgent
  │     ├─ 连接 Milvus 向量数据库
  │     └─ 创建/获取 Collection
  │
  ├─ 3. 从 documents 目录加载文档
  │     ├─ load_documents_from_directory()
  │     │    ├─ 遍历所有 .txt 文件
  │     │    ├─ 读取文件内容
  │     │    ├─ 提取标题（第一行）
  │     │    ├─ 判断文档类别 (category_mapping)
  │     │    └─ split_text() 切分文本
  │     └─ 返回 List[Dict] 文档列表
  │
  ├─ 4. 清理旧 Collection (如果存在) + 重建
  │
  ├─ 5. 将文档添加到 RAG 知识库
  │     └─ rag_agent.add_documents(documents)
  │
  ├─ 6. 打印知识库统计信息
  │     └─ rag_agent.get_stats()
  │
  ├─ 7. 测试知识检索 (3 条测试查询)
  │     └─ rag_agent.search_knowledge(query, top_k=2)
  │
  └─ finally: 清理资源 (rag_agent.close())
```

## 3. 函数详解

### 3.1 `load_rag_agent_class()` -> type

| 项目 | 说明 |
|------|------|
| **作用** | 动态加载同目录下的 `agent.py` 中的 `RAGKnowledgeAgent` 类 |
| **输入** | 无 |
| **输出** | `RAGKnowledgeAgent` 类对象（type） |
| **实现** | 使用 `importlib.util` 从文件路径动态导入模块，避免循环依赖 |

### 3.2 `split_text(text, max_chars=600, overlap=100)` -> `List[str]`

| 项目 | 说明 |
|------|------|
| **作用** | 将长文本按段落切分为多个 chunk |
| **输入** | `text: str` — 原始文档全文 |
| **输入参数** | `max_chars=600` — 每个 chunk 最大字符数; `overlap=100` — 重叠字符数（当前逻辑未实际使用 overlap） |
| **输出** | `List[str]` — 切分后的文本片段列表 |

**切分逻辑**:
1. 按换行符 `\n` 拆分为段落（空行分隔）
2. 逐段累积到 `current_chunk`，直到加入新段落会超限
3. 超限时保存当前 chunk，开始新 chunk
4. 超长段落（> max_chars）按 `max_chars` 硬切分

**数据形状示例**:
```
输入: "段落1\n\n段落2\n\n段落3..."  (长度 ~2000 字符)
输出: ["段落1\n\n段落2", "段落3\n\n段落4", ...]  (每个 <= 600 字符)
```

### 3.3 `load_documents_from_directory(directory_path)` -> `List[Dict]`

| 项目 | 说明 |
|------|------|
| **作用** | 从指定目录加载所有 `.txt` 文档，切分后构建文档对象列表 |
| **输入** | `directory_path: str` — 文档目录的绝对路径 |
| **输出** | `List[Dict]` — 文档对象列表，每个 Dict 代表一个 chunk |

**输出 Dict 结构**:
```python
{
    "id": str,           # 文档 ID，如 "doc_01_1", "doc_01_2" (文件编号 + chunk序号)
    "content": str,      # 切分后的文本内容 (<= 600 字符)
    "metadata": {
        "category": str,       # 文档类别，如 "差旅规定", "报销规定", "预订指南"
        "title": str,          # 标题，格式: "{第一行标题} (Part {N})"
        "source": str,         # 固定值 "商旅知识库文档"
        "file_path": str,      # 源文件的绝对路径
        "version": str,        # 固定值 "2024版"
        "parent_doc": str      # 源文件名，如 "01_travel_standards.txt"
    }
}
```

**类别映射 (`category_mapping`)**:
| 文件名关键词 | 类别 |
|-------------|------|
| `travel_standards` | 差旅规定 |
| `reimbursement_policy` | 报销规定 |
| `booking_guide` | 预订指南 |
| `faq` | FAQ |
| `emergency_procedures` | 应急指南 |
| `platform_guide` | 平台指南 |
| `city_specific_tips` | 城市指南 |
| `environmental_initiatives` | 环保倡议 |
| (其他) | 商旅知识 |

### 3.4 `main()` -> None

**主流程输入/输出**:

| 阶段 | 输入 | 输出 |
|------|------|------|
| 创建模型 | `LLM_CONFIG` (从 config.py 导入) | `OpenAIChatModel` 实例 |
| 初始化 Agent | model, knowledge_base_path, collection_name | `RAGKnowledgeAgent` 实例 |
| 加载文档 | documents_dir 路径 | `List[Dict]` (N 个文档 chunk) |
| 添加到知识库 | documents 列表 | `{"status": "success", "added_count": int, "total_count": int}` |
| 获取统计 | - | `{"status": "success", "collection_name": str, "total_documents": int, ...}` |
| 测试检索 | query (str) | `List[Dict]` — 检索结果列表 |

**测试查询示例**:
```python
test_queries = [
    "出差住宿标准是多少？",
    "航班延误了怎么办？",
    "机票应该提前多久预订？"
]
```

**检索结果结构**:
```python
{
    "metadata": {              # 可能是 str 或 Dict，代码做了兼容处理
        "title": str,          # 文档标题
        "category": str,       # 类别
        ...
    },
    "distance": float          # 向量距离 (越小越相似，1 - distance = 相似度)
}
```

## 4. 外部依赖

| 依赖 | 用途 |
|------|------|
| `config.LLM_CONFIG` | LLM 模型配置 (model_name, api_key, base_url, temperature, max_tokens) |
| `agentscope.model.OpenAIChatModel` | AgentScope 框架的 OpenAI 兼容模型封装 |
| `RAGKnowledgeAgent` (动态加载) | RAG 知识库 Agent，封装了 Milvus 向量数据库操作 |
| Milvus | 向量数据库，用于存储文档嵌入并执行相似度检索 |

## 5. 关键配置参数

```python
# LLM 配置 (来自 config.py)
LLM_CONFIG = {
    "model_name": "...",      # 模型名称
    "api_key": "...",         # API 密钥
    "base_url": "...",        # API 基础 URL
    "temperature": 0.7,       # 温度参数
    "max_tokens": 2000        # 最大 token 数
}

# RAG Agent 配置
collection_name = "business_travel_knowledge"   # Milvus collection 名称
top_k = 3                                        # 默认检索返回数量
embedding_dim = ...                              # 嵌入维度 (由 RAGKnowledgeAgent 定义)

# 文本切分参数
max_chars = 600    # 每个 chunk 最大字符数
overlap = 100      # 重叠字符数 (声明但未实际使用)
```

## 6. 注意事项

1. **Collection 重建**: 脚本每次运行会先删除旧 Collection 再重建，确保数据不会重复/污染
2. **Overlap 未生效**: `split_text` 中声明了 `overlap=100` 参数，但实际按段落切分时并未做 overlap，仅在超长段落硬切分时使用
3. **ID 策略**: 文档 ID 格式为 `doc_{文件编号}_{chunk序号}`，如 `doc_01_1`, `doc_01_2`
4. **资源清理**: 使用 `try/finally` 确保 `rag_agent.close()` 被调用，释放 Milvus 连接
5. **metadata 兼容**: 测试检索时对 metadata 做了 str -> Dict 的兼容处理，说明 Milvus 返回的 metadata 格式可能不一致
