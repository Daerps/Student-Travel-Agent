# RAG 知识库本次修改说明

> 范围：只记录本次围绕 RAG 数据入库、Markdown 支持、Milvus 加载状态修复所做的修改。

## 1. 当前知识库内容

知识文件目录：

```text
.claude/skills/ask-question/data/documents
```

当前知识库文件大致分为三类：

### 1.1 操作型知识 txt

这些文件用于指导大模型“如何规划、如何提问、如何组织回复”，不是正式制度依据。

```text
01_trip_planning_checklist.txt
02_reimbursement_materials_workflow.txt
03_transport_and_hotel_booking_playbook.txt
04_business_travel_faq_tongji.txt
05_emergency_handling_playbook.txt
06_tongji_reimbursement_system_workflow.txt
07_city_travel_tips_for_conference.txt
08_green_and_cost_saving_travel_tips.txt
```

作用：

1. 让模型先判断用户是个人旅游、国内差旅、参会、科研差旅还是因公出国。
2. 引导模型按“正式制度 -> 操作指南 -> 行程输出”的顺序检索。
3. 补充交通住宿预订、报销材料、异常处理、城市出行经验等规划能力。
4. 明确个人旅游、自费段、景区门票、导游费等不能混入公务报销。

### 1.2 制度型知识 md

这些文件主要来自同济大学财务/差旅/报销相关资料，用于提供制度依据。

典型文件包括：

```text
tongji_travel_expense_management_rules_2024_revised_2026.md
tongji_conference_expense_management_rules_2024.md
tongji_research_travel_lodging_package_and_uplift_rules_2021.md
tongji_eight_point_rules_financial_prohibitions_80_items_2024.md
tongji_overseas_lodging_meals_misc_standards_2019.md
tongji_overseas_travel_foreign_affairs_approval_items_2016.md
同济大学报销手册.md
同济大学因公临时出国管理办法-修正.md
```

其中：

1. `同济大学报销手册.md` 由 `同济大学报销手册.pdf` 转换得到。
2. `同济大学因公临时出国管理办法-修正.md` 由同名 PDF 转换得到，属于旧版/历史参考文件。
3. 如果同一份资料同时存在中文名和英文名 md，初始化脚本会通过内容哈希跳过重复内容，避免重复入库。

### 1.3 原始 PDF

PDF 保留为原始依据，不直接在运行时解析入库。

```text
同济大学报销手册.pdf
同济大学因公临时出国管理办法-修正.pdf
财务报销培训.pdf
```

本次只将前两个 PDF 转换为 Markdown。`财务报销培训.pdf` 暂时不处理。

## 2. init_knowledge_base.py 的修改

文件路径：

```text
.claude/skills/ask-question/script/init_knowledge_base.py
```

### 2.1 支持读取 txt 和 md

原逻辑只读取：

```python
doc_dir.glob("*.txt")
```

现在改为同时读取：

```python
supported_suffixes = {".txt", ".md"}
```

这样操作型 txt 和制度型 md 都可以进入 Milvus 向量库。

### 2.2 提取标题

新增 `get_document_title()`：

1. 如果 Markdown 有 frontmatter，优先读取 `title:`。
2. 如果有 Markdown 标题行，读取 `# ...`。
3. 否则使用首个非空行。
4. 都没有时使用文件名。

这样 RAG 返回结果中的 `metadata.title` 更清晰。

### 2.3 轻量 metadata

当前 metadata 保留基础溯源字段：

```python
category
format
status
title
source
file_path
parent_doc
chunk_index
chunk_count
```

说明：

1. 已去掉 `priority`。
2. 已去掉 `.md/.txt` 的 `policy/playbook` 类型区分。
3. `.md` 和 `.txt` 都统一当作知识文本检索。
4. `format` 只用于记录原始文件格式，不参与检索逻辑。

### 2.4 内容哈希去重

新增内容哈希：

```python
content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
```

目的：

1. 避免中文名和英文名的同一份 Markdown 被重复切分。
2. 避免 Milvus 中出现大量重复 chunk。

### 2.5 重建 collection

初始化脚本仍保留原有逻辑：

1. 如果 collection 已存在，先 drop。
2. 再 create。
3. 再将新切分的文档重新 insert。

也就是说，重新运行初始化脚本会重建 RAG 向量库，不是追加。

## 3. agent.py 的修改

文件路径：

```text
.claude/skills/ask-question/script/agent.py
```

### 3.1 修复 Milvus collection released 问题

报错现象：

```text
Collection 'business_travel_knowledge' is in state 'released'; call load() before search/get/query
```

原因：

1. Milvus collection 存在不等于已经 load。
2. 原代码只判断 `has_collection()`，没有调用 `load_collection()`。
3. 搜索时 collection 处于 `released` 状态，所以 `search()` 报错。

### 3.2 新增 _load_collection()

新增方法：

```python
def _load_collection(self):
    if self.milvus_client.has_collection(self.collection_name):
        self.milvus_client.load_collection(self.collection_name)
```

并用 `try/except` 包住，避免不同 Milvus Lite 版本行为差异导致程序中断。

### 3.3 调用位置

现在在以下位置确保 collection 可检索：

1. RAG Agent 初始化完成后。
2. Milvus 客户端重连后。
3. 文档插入后。
4. 搜索前。

文档插入后还新增了：

```python
self.milvus_client.flush(self.collection_name)
```

作用是尽量确保插入数据落盘后再 load/search。

## 4. 当前运行流程

重建 RAG 知识库：

```powershell
cd "D:\Algorithm\for study\travel agent"
python ".claude\skills\ask-question\script\init_knowledge_base.py"
```

执行后流程：

1. 创建 LLM model。
2. 初始化 `RAGKnowledgeAgent`。
3. 读取 `documents` 下的 `.txt` 和 `.md`。
4. 按段落切分。
5. 删除旧 Milvus collection。
6. 创建新 collection。
7. 写入 embedding 和 metadata。
8. flush/load collection。
9. 执行测试检索。

## 5. 注意事项

1. PDF 不直接入库，先转 Markdown 再入库。
2. 如果同一份内容存在多个文件名，脚本会跳过重复内容。
3. `No sentence-transformers model found... Creating a new one with mean pooling` 不是 RAG 读取失败，而是本地 embedding 模型目录不是标准 sentence-transformers 包格式。
4. 如果运行后回答仍用旧知识，优先检查是否重新运行了 `init_knowledge_base.py`。
5. 当前 RAG collection 名仍是：

```text
business_travel_knowledge
```

