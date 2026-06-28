# Travel Agent Evaluation Dataset Schema

本文档定义 `tests/eval_dataset_100.json` 的人工标注格式。所有样本都使用统一结构；不参与某项评估的模块使用 `eval: false`，评测脚本读取时会跳过。

## 1. Agent 映射

人工标注 `gold.required_agents` 时使用数字，评测时再映射为项目内部 `agent_schedule.agent_name`。

| 编号 | agent_name | 含义 |
| --- | --- | --- |
| 1 | `event_collection` | 事项/行程信息收集 |
| 2 | `itinerary_planning` | 行程规划 |
| 3 | `preference` | 偏好管理 |
| 4 | `memory_query` | 历史记忆查询 |
| 5 | `rag_knowledge` | RAG 知识库问答 |
| 6 | `information_query` | 实时信息查询 |

意图识别采用召回式成功标准：

```text
GoldAgents subset of PredAgents -> success
```

也就是人工标注中要求触发的 Agent 全部出现在系统实际调度结果中，即认为该轮意图识别成功；额外多调度的 Agent 不扣分。

## 2. RAG 证据簇映射

人工标注 `gold.rag.gold_cluster` 时使用数字。若一个问题可能命中多个合理文件类型，可以填写数组，评测时按 OR 判断。

| 编号 | 证据簇 | 主要覆盖内容 |
| --- | --- | --- |
| 1 | 国内差旅制度类 | 国内出差住宿、交通、伙食补助、市内交通、报销范围、超标处理 |
| 2 | 报销材料与系统流程类 | 票据材料、报销单填写、系统提交、退回原因、材料顺序 |
| 3 | 会议费制度类 | 会议费开支范围、会议审批、会议住宿/伙食/其他费用标准、会议费报销材料 |
| 4 | 因公出国/出境类 | 因公出国审批、住宿费/伙食费/公杂费标准、外事审批事项、境外票据说明 |
| 5 | 合规禁止事项类 | 八项规定、不得报销事项、个人旅游、宴请、娱乐、超标准、与公务无关费用 |
| 6 | 异常与辅助出行类 | 航班延误、取消、证件遗失、票据遗失、突发情况处理 |

RAG 评估包含三个指标：

```text
Cluster Hit@K:
Top-K 检索结果中，至少一个 chunk 所属文件类型命中 gold_cluster。

Evidence Hit@K:
Top-K 检索结果中，至少一个 chunk 与 gold_evidence 的语义相似度 >= similarity_threshold。

Strict Hit@K:
Top-K 检索结果中，至少一个 chunk 同时满足 Cluster Hit 和 Evidence Hit。
```

其中 `gold_evidence` 不是关键词组，而是一句或一段“看到该问题后希望检索到的目标语义”。评测脚本会用本地 embedding 模型计算检索 chunk 与 `gold_evidence` 的余弦相似度。

## 3. 统一样本格式

```json
{
  "id": "case_061",
  "user_id": "u003",
  "turn_id": 1,
  "query": "我下周从上海去北京参加学术会议，帮我查会议费报销材料，再规划一下行程。",
  "gold": {
    "required_agents": [1, 2, 5],
    "preference": {
      "eval": false
    },
    "rag": {
      "eval": true,
      "gold_cluster": [2, 3],
      "gold_evidence": "会议费报销一般需要会议通知、会议审批材料、参会人员信息、会议费用发票、费用明细和结算材料等。",
      "similarity_threshold": 0.65,
      "top_k": 3
    }
  }
}
```

## 4. 字段说明

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 样本编号，建议使用 `case_001` 形式 |
| `user_id` | string | 是 | 用户编号；同一用户按 `turn_id` 顺序执行 |
| `turn_id` | integer | 是 | 同一用户的第几轮输入 |
| `query` | string | 是 | 当前轮用户输入 |
| `gold.required_agents` | integer[] | 是 | 必须触发的 Agent 编号列表 |
| `gold.preference.eval` | boolean | 是 | 是否纳入偏好行为评估 |
| `gold.rag.eval` | boolean | 是 | 是否纳入 RAG 检索评估 |
| `gold.rag.gold_cluster` | integer 或 integer[] | 当 `rag.eval=true` 时必填 | 目标 RAG 证据簇编号；数组按 OR 判断，命中任意一个即算 cluster hit |
| `gold.rag.gold_evidence` | string | 当 `rag.eval=true` 时必填 | 目标语义描述，用于和 Top-K chunk 计算语义相似度 |
| `gold.rag.similarity_threshold` | number | 当 `rag.eval=true` 时必填 | 语义相似度阈值，当前默认 `0.65` |
| `gold.rag.top_k` | integer | 当 `rag.eval=true` 时必填 | 参与评估的 Top-K 检索结果数量，当前默认 `3` |

## 5. 非 RAG 样本

不评估 RAG 时，保持结构统一，只填写：

```json
{
  "gold": {
    "required_agents": [3],
    "preference": {
      "eval": true
    },
    "rag": {
      "eval": false
    }
  }
}
```

## 6. 标注注意事项

- 所有样本都必须填写 `required_agents`，因为 100 条样本都会参与意图识别评估。
- `preference.eval=true` 只表示该轮应该触发偏好行为，不需要额外标注 `set / append / replace`。
- `rag.eval=false` 时不要填写 `gold_cluster`、`gold_evidence`、`similarity_threshold`、`top_k`。
- `gold_cluster` 可以写单个数字，也可以写多个数字，例如 `[1, 2]`；多个数字表示这些文件类型都可接受。
- 缓存命中率不需要人工真值，由评测运行时统计 Redis 中短期记忆、用户偏好、长期摘要三类缓存的 hit/miss。

## 7. RAG Review 文件格式

`tests/rag_evidence_review.json` 用于单独审阅和测试 RAG 语义目标，不会修改 `eval_dataset_100.json`。评测时使用：

```bash
python tests/run_eval_100.py --rag-review
```

其中 `review_search_query` 会被当作 RAG 的目标语义文本，等价于正式样本中的 `gold_evidence`。

```json
{
  "id": "case_030",
  "user_id": "u003",
  "turn_id": 4,
  "query": "我在申请会议，会议学校是能报销是吗",
  "gold_cluster": [3, 2],
  "review_search_query": "学校会议费用是否能报销一般由会议申请审批、会议性质、经费来源、费用范围和票据材料等组成。"
}
```

`gold_cluster` 同样支持单个数字或数字数组；数组按 OR 判断，命中任意一个类别即算 `Cluster Hit@K`。
