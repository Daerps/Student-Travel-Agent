"""
RAG知识库智能体 RAGKnowledgeAgent
职责：基于向量数据库的知识检索与问答

核心功能：
1. 知识库构建：将商旅相关文档向量化并存储到Milvus Lite
2. 语义检索：根据用户查询检索最相关的知识片段
3. 知识问答：结合检索到的知识和LLM生成准确答案
4. 知识管理：支持添加、更新、删除知识库内容

技术栈：
- Milvus Lite: 轻量级向量数据库（本地存储）
- sentence-transformers: 文本向量化模型
- LLM: 用户配置的豆包模型用于生成答案

安装：
pip install milvus sentence-transformers
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List, Dict, Any
from collections import Counter
import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path

# Add project root to sys.path
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))
from utils.langsmith_tracing import start_trace

_GRPC_MAX_MS = '2147483647'  # gRPC 使用的 int32 上限，约 24.8 天
os.environ['GRPC_KEEPALIVE_TIME_MS'] = _GRPC_MAX_MS
os.environ['GRPC_KEEPALIVE_TIMEOUT_MS'] = '20000'
os.environ['GRPC_KEEPALIVE_PERMIT_WITHOUT_CALLS'] = '0'
os.environ['GRPC_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS'] = _GRPC_MAX_MS
os.environ['GRPC_HTTP2_MIN_PING_INTERVAL_WITHOUT_DATA_MS'] = _GRPC_MAX_MS

logger = logging.getLogger(__name__)

try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    jieba = None
    JIEBA_AVAILABLE = False

try:
    from pymilvus import MilvusClient, DataType
    from sentence_transformers import SentenceTransformer
    DEPENDENCIES_AVAILABLE = True
except ImportError as e:
    logger.warning(f"RAG dependencies not available: {e}")
    logger.warning("Install with: pip install pymilvus sentence-transformers")
    DEPENDENCIES_AVAILABLE = False


class RAGKnowledgeAgent(AgentBase):
    """RAG知识库智能体"""

    def __init__(
        self,
        name: str = "RAGKnowledgeAgent",
        model=None,
        knowledge_base_path: str = None,
        collection_name: str = "business_travel_knowledge",
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
        top_k: int = 3,
        **kwargs
    ):
        super().__init__()
        self.name = name
        self.model = model
        
        if knowledge_base_path is None:
            # Default to local data directory in skill folder
            current_dir = Path(__file__).parent.parent
            knowledge_base_path = str(current_dir / "data" / "rag_knowledge")

        self.knowledge_base_path = Path(knowledge_base_path)
        self.collection_name = collection_name
        self.top_k = top_k
        self.vector_top_n = int(kwargs.get("vector_top_n", max(top_k * 4, 10)))
        self.bm25_top_n = int(kwargs.get("bm25_top_n", max(top_k * 4, 10)))
        self.rrf_k = int(kwargs.get("rrf_k", 60))
        self._bm25_documents = []
        self._bm25_doc_freqs = []
        self._bm25_doc_lens = []
        self._bm25_idf = {}
        self._bm25_avgdl = 0.0
        from utils.skill_loader import SkillLoader
        self.skill_loader = SkillLoader()

        if not DEPENDENCIES_AVAILABLE:
            logger.error("RAG dependencies not installed. Install with: pip install pymilvus sentence-transformers")
            self.initialized = False
            return

        # 优先使用 config 中的配置（支持本地路径，避免连 HuggingFace）
        try:
            from config import RAG_CONFIG
            embedding_model = RAG_CONFIG.get("embedding_model", embedding_model)
        except Exception:
            pass

        # 若配置的是本地路径且存在，则从本地加载，否则按模型 ID 使用（会联网）
        model_path_or_id = embedding_model
        path_obj = Path(embedding_model).expanduser()
        if not path_obj.is_absolute():
            path_obj = Path.cwd() / path_obj
        if path_obj.exists():
            model_path_or_id = str(path_obj.resolve())
            logger.info(f"Using local embedding model: {model_path_or_id}")
        else:
            if "/" in embedding_model or "\\" in embedding_model or embedding_model.startswith("."):
                logger.warning(
                    f"Configured embedding path does not exist: {embedding_model}，将使用 BAAI/bge-small-zh-v1.5 并尝试联网下载。"
                )
                model_path_or_id = "BAAI/bge-small-zh-v1.5"
        logger.info(f"Loading embedding model: {model_path_or_id}")
        self.embedding_model = SentenceTransformer(model_path_or_id)
        self.embedding_dim = self.embedding_model.get_sentence_embedding_dimension()

        # 初始化 Milvus Lite（本地文件存储）
        milvus_db_path = str(self.knowledge_base_path / "milvus_lite.db")
        logger.info(f"Initializing Milvus Lite at: {milvus_db_path}")

        self.milvus_client = MilvusClient(milvus_db_path, grpc_options={"keepalive_time": _GRPC_MAX_MS, "keepalive_timeout": "20000", "keepalive_permit_without_calls": "0", "http2_min_recv_ping_interval_without_data": _GRPC_MAX_MS, "http2_min_ping_interval_without_data": _GRPC_MAX_MS})
        self._client_created_at = None  # 用于追踪客户端创建时间

        # 检查collection是否存在
        if self.milvus_client.has_collection(collection_name):
            logger.info(f"Loaded existing collection: {collection_name}")
        else:
            # 创建新collection
            logger.info(f"Creating new collection: {collection_name}")
            self.milvus_client.create_collection(
                collection_name=collection_name,
                dimension=self.embedding_dim,
                metric_type="COSINE",  # 余弦相似度
                auto_id=False,
            )
            logger.info(f"Created new collection: {collection_name}")

        self.initialized = True
        self._milvus_db_path = milvus_db_path  # 保存路径用于重连
        self._load_collection()
        self._load_bm25_corpus()
        logger.info("RAG Knowledge Agent (Milvus Lite) initialized successfully")

    def _load_collection(self):
        """确保 collection 处于可检索状态。"""
        if not hasattr(self, "milvus_client"):
            return

        try:
            if self.milvus_client.has_collection(self.collection_name):
                self.milvus_client.load_collection(self.collection_name)
                logger.debug(f"Milvus collection loaded: {self.collection_name}")
        except Exception as e:
            logger.warning(f"Failed to load Milvus collection {self.collection_name}: {e}")

    def _load_bm25_corpus(self):
        """Load chunk text for BM25. Prefer chunks.json, fall back to source files."""
        self._bm25_documents = []
        self._bm25_doc_freqs = []
        self._bm25_doc_lens = []
        self._bm25_idf = {}
        self._bm25_avgdl = 0.0

        documents = self._read_chunks_manifest()
        if not documents:
            documents = self._read_source_documents_as_chunks()

        seen_keys = set()
        doc_freq = Counter()
        tokenized_docs = []

        for doc in documents:
            content = doc.get("content", "")
            metadata = doc.get("metadata", {}) or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}
            if not content:
                continue

            key = self._document_key({"metadata": metadata, "content": content})
            if key in seen_keys:
                continue
            seen_keys.add(key)

            tokens = self._tokenize_for_bm25(content)
            if not tokens:
                continue

            token_counts = Counter(tokens)
            tokenized_docs.append((token_counts, len(tokens)))
            doc_freq.update(token_counts.keys())
            self._bm25_documents.append({
                "id": doc.get("id", key),
                "content": content,
                "metadata": metadata,
            })

        total_docs = len(self._bm25_documents)
        if not total_docs:
            logger.warning("BM25 corpus is empty; RAG retrieval will use vector search only")
            return

        self._bm25_doc_freqs = [item[0] for item in tokenized_docs]
        self._bm25_doc_lens = [item[1] for item in tokenized_docs]
        self._bm25_avgdl = sum(self._bm25_doc_lens) / max(total_docs, 1)
        self._bm25_idf = {
            token: math.log(1 + (total_docs - freq + 0.5) / (freq + 0.5))
            for token, freq in doc_freq.items()
        }
        logger.info(f"BM25 corpus loaded: {total_docs} chunks")

    def _read_chunks_manifest(self) -> List[Dict[str, Any]]:
        manifest_path = self.knowledge_base_path / "chunks.json"
        if not manifest_path.exists():
            return []

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            chunks = data.get("chunks", []) if isinstance(data, dict) else data
            return chunks if isinstance(chunks, list) else []
        except Exception as e:
            logger.warning(f"Failed to load BM25 chunks manifest {manifest_path}: {e}")
            return []

    def _read_source_documents_as_chunks(self) -> List[Dict[str, Any]]:
        documents_dir = self.knowledge_base_path.parent / "documents"
        if not documents_dir.exists():
            return []

        documents = []
        for file_path in sorted(documents_dir.iterdir()):
            if not file_path.is_file() or file_path.suffix.lower() not in {".txt", ".md"}:
                continue
            try:
                content = file_path.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning(f"Failed to read BM25 source document {file_path}: {e}")
                continue
            if not content:
                continue

            chunks = self._split_text_for_bm25(content)
            title = self._extract_title(content, file_path)
            safe_stem = "".join(ch if ch.isalnum() else "_" for ch in file_path.stem)
            for index, chunk_content in enumerate(chunks, 1):
                chunk_uid = f"doc_{safe_stem}_{index}"
                documents.append({
                    "id": chunk_uid,
                    "content": chunk_content,
                    "metadata": {
                        "chunk_uid": chunk_uid,
                        "title": f"{title} (Part {index})",
                        "source": "source_documents",
                        "file_path": str(file_path),
                        "parent_doc": file_path.name,
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                    },
                })
        return documents

    def _split_text_for_bm25(self, text: str, max_chars: int = 600, overlap: int = 100) -> List[str]:
        paragraphs = []
        current_para = []
        for line in text.split("\n"):
            if line.strip() == "":
                if current_para:
                    paragraphs.append("\n".join(current_para))
                    current_para = []
            else:
                current_para.append(line)
        if current_para:
            paragraphs.append("\n".join(current_para))

        chunks = []
        current_chunk = ""
        for para in paragraphs:
            if len(current_chunk) + len(para) <= max_chars:
                current_chunk += "\n\n" + para
                continue

            if current_chunk:
                chunks.append(current_chunk.strip())

            if len(para) > max_chars:
                remaining = para
                while len(remaining) > max_chars:
                    chunks.append(remaining[:max_chars])
                    remaining = remaining[max_chars - overlap:]
                current_chunk = remaining
            else:
                current_chunk = para

        if current_chunk:
            chunks.append(current_chunk.strip())
        return chunks

    def _extract_title(self, content: str, file_path: Path) -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped == "---":
                continue
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or file_path.stem
            if stripped.startswith("title:"):
                return stripped.split(":", 1)[1].strip().strip('"').strip("'") or file_path.stem
            return stripped
        return file_path.stem

    def _tokenize_for_bm25(self, text: str) -> List[str]:
        tokens = []
        for segment in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]+", text.lower()):
            if re.fullmatch(r"[\u4e00-\u9fff]+", segment):
                if JIEBA_AVAILABLE:
                    tokens.extend(jieba.cut_for_search(segment))
                else:
                    tokens.extend(segment)
                    tokens.extend(segment[i:i + 2] for i in range(max(len(segment) - 1, 0)))
            else:
                tokens.append(segment)
        return [token for token in tokens if token.strip()]

    def _document_key(self, doc: Dict[str, Any]) -> str:
        metadata = doc.get("metadata", {}) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}

        chunk_uid = metadata.get("chunk_uid")
        if chunk_uid:
            return str(chunk_uid)

        parent_doc = metadata.get("parent_doc")
        chunk_index = metadata.get("chunk_index")
        if parent_doc is not None and chunk_index is not None:
            return f"{parent_doc}::{chunk_index}"

        doc_id = doc.get("id")
        if doc_id not in (None, ""):
            return str(doc_id)

        content = doc.get("content", "")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _vector_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        query_embedding = self.embedding_model.encode(query).tolist()
        results = self.milvus_client.search(
            collection_name=self.collection_name,
            data=[query_embedding],
            limit=limit,
            output_fields=["id", "content", "metadata"],
        )

        retrieved_docs = []
        if results and len(results) > 0:
            for rank, hit in enumerate(results[0], 1):
                metadata_str = hit.get("entity", {}).get("metadata", "{}")
                try:
                    metadata = json.loads(metadata_str)
                except Exception:
                    metadata = {}

                retrieved_docs.append({
                    "id": hit.get("entity", {}).get("id", ""),
                    "content": hit.get("entity", {}).get("content", ""),
                    "metadata": metadata,
                    "distance": hit.get("distance", 0.0),
                    "vector_rank": rank,
                    "retrieval_sources": ["vector"],
                })
        return retrieved_docs

    def _bm25_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        if not self._bm25_documents:
            return []

        query_tokens = set(self._tokenize_for_bm25(query))
        if not query_tokens:
            return []

        k1 = 1.5
        b = 0.75
        scores = []
        avgdl = self._bm25_avgdl or 1.0

        for index, doc_freqs in enumerate(self._bm25_doc_freqs):
            doc_len = self._bm25_doc_lens[index]
            score = 0.0
            for token in query_tokens:
                tf = doc_freqs.get(token, 0)
                if not tf:
                    continue
                idf = self._bm25_idf.get(token, 0.0)
                denominator = tf + k1 * (1 - b + b * doc_len / avgdl)
                score += idf * (tf * (k1 + 1)) / denominator

            if score > 0:
                doc = dict(self._bm25_documents[index])
                doc["bm25_score"] = score
                scores.append(doc)

        scores.sort(key=lambda item: item["bm25_score"], reverse=True)
        for rank, doc in enumerate(scores[:limit], 1):
            doc["bm25_rank"] = rank
            doc["retrieval_sources"] = ["bm25"]
        return scores[:limit]

    def _rrf_fusion(
        self,
        vector_docs: List[Dict[str, Any]],
        bm25_docs: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        fused = {}

        def add_doc(doc: Dict[str, Any], source: str, rank: int):
            key = self._document_key(doc)
            if key not in fused:
                fused[key] = dict(doc)
                fused[key]["rrf_score"] = 0.0
                fused[key]["retrieval_sources"] = set()
            else:
                existing = fused[key]
                for field in ("distance", "bm25_score", "vector_rank", "bm25_rank"):
                    if field in doc and field not in existing:
                        existing[field] = doc[field]

            fused[key]["rrf_score"] += 1.0 / (self.rrf_k + rank)
            fused[key]["retrieval_sources"].add(source)

        for rank, doc in enumerate(vector_docs, 1):
            add_doc(doc, "vector", rank)
        for rank, doc in enumerate(bm25_docs, 1):
            add_doc(doc, "bm25", rank)

        fused_docs = list(fused.values())
        for doc in fused_docs:
            doc["retrieval_sources"] = sorted(doc["retrieval_sources"])

        fused_docs.sort(
            key=lambda item: (
                item.get("rrf_score", 0.0),
                -min(item.get("vector_rank", 10**6), item.get("bm25_rank", 10**6)),
            ),
            reverse=True,
        )
        return fused_docs[:top_k]

    def _ensure_connection(self):
        """确保 Milvus 连接正常，如果需要则重新创建客户端"""
        try:
            # 尝试一个轻量级操作来检查连接
            self.milvus_client.has_collection(self.collection_name)
        except Exception as e:
            logger.warning(f"Milvus connection issue detected: {e}, reconnecting...")
            try:
                # 关闭旧连接
                if hasattr(self.milvus_client, 'close'):
                    try:
                        self.milvus_client.close()
                    except:
                        pass

                # 重新创建客户端
                self.milvus_client = MilvusClient(self._milvus_db_path)
                logger.info("Milvus client reconnected successfully")
            except Exception as reconnect_error:
                logger.error(f"Failed to reconnect Milvus: {reconnect_error}")
                raise

        self._load_collection()

    def add_documents(self, documents: List[Dict[str, str]]) -> Dict:
        """
        添加文档到知识库

        Args:
            documents: 文档列表，每个文档包含 {'content': '内容', 'metadata': {...}}

        Returns:
            添加结果统计
        """
        if not self.initialized:
            return {"status": "error", "message": "RAG Agent not initialized"}

        try:
            # 确保连接正常
            self._ensure_connection()
            # 获取当前文档总数，用于生成连续的ID
            stats = self.milvus_client.get_collection_stats(self.collection_name)
            current_count = stats.get("row_count", 0)

            # 准备数据
            data_to_insert = []

            for i, doc in enumerate(documents):
                # Milvus 要求 id 必须是 int64
                doc_id = current_count + i + 1
                content = doc['content']
                metadata = doc.get('metadata', {})

                # 生成向量
                embedding = self.embedding_model.encode(content).tolist()

                # Milvus 数据格式
                data_to_insert.append({
                    "id": doc_id,
                    "vector": embedding,
                    "content": content,
                    "metadata": json.dumps(metadata, ensure_ascii=False)  # 将metadata转为JSON字符串
                })

            # 批量插入到 Milvus
            self.milvus_client.insert(
                collection_name=self.collection_name,
                data=data_to_insert
            )
            try:
                self.milvus_client.flush(self.collection_name)
            except Exception as flush_error:
                logger.debug(f"Milvus flush skipped or failed: {flush_error}")
            self._load_collection()
            self._load_bm25_corpus()

            # 获取总数
            stats = self.milvus_client.get_collection_stats(self.collection_name)
            total_count = stats.get("row_count", len(documents))

            logger.info(f"Successfully added {len(documents)} documents to knowledge base")
            return {
                "status": "success",
                "added_count": len(documents),
                "total_count": total_count
            }

        except Exception as e:
            logger.error(f"Error adding documents: {e}")
            return {"status": "error", "message": str(e)}

    def _legacy_vector_search_knowledge(self, query: str, top_k: Optional[int] = None) -> List[Dict]:
        """
        检索知识库

        Args:
            query: 查询文本
            top_k: 返回top k个结果

        Returns:
            检索结果列表
        """
        if not self.initialized:
            return []

        k = top_k or self.top_k
        search_trace = start_trace(
            "rag.search_knowledge",
            inputs={
                "query": query,
                "top_k": k,
                "collection_name": self.collection_name,
            },
            metadata={"agent_name": self.name},
        )

        try:
            # 确保连接正常
            self._ensure_connection()
            self._load_collection()

            # 生成查询向量
            query_embedding = self.embedding_model.encode(query).tolist()

            # 在 Milvus 中检索
            results = self.milvus_client.search(
                collection_name=self.collection_name,
                data=[query_embedding],
                limit=k,
                output_fields=["id", "content", "metadata"]
            )

            # 格式化结果
            retrieved_docs = []
            if results and len(results) > 0:
                for hit in results[0]:
                    # 解析metadata
                    metadata_str = hit.get("entity", {}).get("metadata", "{}")
                    try:
                        metadata = json.loads(metadata_str)
                    except:
                        metadata = {}

                    retrieved_docs.append({
                        'id': hit.get("entity", {}).get("id", ""),
                        'content': hit.get("entity", {}).get("content", ""),
                        'metadata': metadata,
                        'distance': hit.get("distance", 0.0)
                    })

            logger.info(f"Retrieved {len(retrieved_docs)} documents for query: {query[:50]}")
            search_trace.end({
                "status": "success",
                "document_count": len(retrieved_docs),
                "documents": [
                    {
                        "title": doc.get("metadata", {}).get("title"),
                        "source": doc.get("metadata", {}).get("source"),
                        "distance": doc.get("distance"),
                        "content_preview": doc.get("content", "")[:200],
                    }
                    for doc in retrieved_docs
                ],
            })
            return retrieved_docs

        except Exception as e:
            logger.error(f"Error searching knowledge: {e}")
            search_trace.end_error(e, outputs={"status": "error", "query": query})
            return []

    def search_knowledge(self, query: str, top_k: Optional[int] = None) -> List[Dict]:
        """Hybrid retrieval: Milvus COSINE vector search + BM25 + RRF fusion."""
        if not self.initialized:
            return []

        k = top_k or self.top_k
        search_trace = start_trace(
            "rag.search_knowledge",
            inputs={
                "query": query,
                "top_k": k,
                "collection_name": self.collection_name,
                "retrieval_mode": "hybrid_vector_bm25_rrf",
            },
            metadata={"agent_name": self.name},
        )

        try:
            self._ensure_connection()
            self._load_collection()

            vector_docs = self._vector_search(query, limit=max(self.vector_top_n, k))
            bm25_docs = self._bm25_search(query, limit=max(self.bm25_top_n, k))
            retrieved_docs = self._rrf_fusion(vector_docs, bm25_docs, top_k=k)

            logger.info(
                "Hybrid retrieval returned %s docs for query: %s "
                "(vector_candidates=%s, bm25_candidates=%s)",
                len(retrieved_docs),
                query[:50],
                len(vector_docs),
                len(bm25_docs),
            )
            search_trace.end({
                "status": "success",
                "retrieval_mode": "hybrid_vector_bm25_rrf",
                "document_count": len(retrieved_docs),
                "vector_candidates": len(vector_docs),
                "bm25_candidates": len(bm25_docs),
                "documents": [
                    {
                        "title": doc.get("metadata", {}).get("title"),
                        "source": doc.get("metadata", {}).get("source"),
                        "distance": doc.get("distance"),
                        "bm25_score": doc.get("bm25_score"),
                        "rrf_score": doc.get("rrf_score"),
                        "retrieval_sources": doc.get("retrieval_sources", []),
                        "content_preview": doc.get("content", "")[:200],
                    }
                    for doc in retrieved_docs
                ],
            })
            return retrieved_docs

        except Exception as e:
            logger.error(f"Error searching knowledge: {e}")
            search_trace.end_error(e, outputs={"status": "error", "query": query})
            return []

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """
        RAG问答主流程
        1. 接收用户查询
        2. 检索相关知识
        3. 结合知识生成答案
        """
        if not self.initialized:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "error",
                    "message": "RAG Agent not initialized. Please install dependencies: pip install pymilvus sentence-transformers"
                }),
                role="assistant"
            )

        if x is None:
            return Msg(name=self.name, content=json.dumps({}), role="assistant")

        # 获取用户查询
        if isinstance(x, list):
            content = x[-1].content if x else ""
        else:
            content = x.content

        # 尝试解析 JSON 输入 (来自 Orchestrator)
        user_query = content
        if isinstance(content, str) and content.strip().startswith('{'):
            try:
                import json
                data = json.loads(content)
                # 只要解析成功，就认为 content 是结构化数据，尝试提取 query
                extracted_query = ""
                if "context" in data and isinstance(data["context"], dict):
                    extracted_query = data["context"].get("rewritten_query", "")
                elif "rewritten_query" in data:
                    extracted_query = data.get("rewritten_query", "")
                
                # 使用提取到的 query（即使为空，也比 JSON 字符串好）
                user_query = extracted_query
            except:
                pass  # 解析失败则保留原字符串

        # 检索相关知识
        retrieved_docs = self.search_knowledge(user_query)

        if not retrieved_docs:
            result = {
                "status": "no_knowledge",
                "query": user_query,
                "answer": "抱歉，我在知识库中没有找到相关信息。",
                "retrieved_documents": []
            }
            return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

        # 构建知识上下文
        knowledge_context = "\n\n".join([
            f"【知识片段{i+1}】\n{doc['content']}"
            for i, doc in enumerate(retrieved_docs)
        ])

        # 如果有LLM，使用LLM生成答案
        if self.model:
            generation_trace = start_trace(
                "rag.generate_answer",
                inputs={
                    "query": user_query,
                    "document_count": len(retrieved_docs),
                    "documents": [
                        {
                            "title": doc.get("metadata", {}).get("title"),
                            "source": doc.get("metadata", {}).get("source"),
                            "distance": doc.get("distance"),
                        }
                        for doc in retrieved_docs
                    ],
                },
                metadata={"agent_name": self.name},
            )
            # 动态读取 Prompt 指令 (Progressive Disclosure)
            skill_instruction = self.skill_loader.get_skill_content("ask-question")
            if not skill_instruction:
                skill_instruction = "请基于知识库中的信息回答用户的问题。"

            prompt = f"""你是一个学生出差/旅游知识专家。请严格基于以下知识库中的信息回答用户的问题。

【用户问题】
{user_query}

【知识库信息】
{knowledge_context}

【任务说明】
{skill_instruction}

【重要约束】
1. 如果【知识库信息】中没有包含回答用户问题所需的信息，请直接回答“抱歉，知识库中没有找到相关信息”，不要尝试根据你自己的知识编造答案。
2. 即使问题很基础，如果知识库里没写，就说不知道。
3. 请以专业、客观的语气回答。
"""

            try:
                # 调用LLM生成答案
                messages = [
                    {"role": "system", "content": "你是一个商旅知识专家。"},
                    {"role": "user", "content": prompt}
                ]
                response = await self.model(messages)

                # 获取响应内容 - 处理异步生成器
                answer = ""
                if hasattr(response, '__aiter__'):
                    # 异步生成器，需要迭代获取内容
                    async for chunk in response:
                        if isinstance(chunk, str):
                            answer = chunk
                        elif hasattr(chunk, 'content'):
                            if isinstance(chunk.content, str):
                                answer = chunk.content
                            elif isinstance(chunk.content, list):
                                for item in chunk.content:
                                    if isinstance(item, dict) and item.get('type') == 'text':
                                        answer = item.get('text', '')
                elif hasattr(response, 'text'):
                    answer = response.text
                elif hasattr(response, 'content'):
                    answer = response.content
                elif isinstance(response, dict) and 'content' in response:
                    answer = response['content']
                else:
                    answer = str(response) if response else "无法生成答案"

                if not answer:
                    answer = "无法生成答案"
                
                # 清理 LLM 可能输出的 JSON 格式
                answer_str = answer.strip()
                if answer_str.startswith("{") and answer_str.endswith("}"):
                    try:
                        import json
                        json_obj = json.loads(answer_str)
                        # 如果 LLM 输出了 {"answer": "..."} 或 {"content": "..."}
                        if isinstance(json_obj, dict):
                            answer = json_obj.get("answer") or json_obj.get("content") or answer
                    except:
                        pass
                generation_trace.end({
                    "status": "success",
                    "answer_preview": answer[:500] if isinstance(answer, str) else str(answer)[:500],
                })

            except Exception as e:
                logger.error(f"Error generating answer with LLM: {e}")
                generation_trace.end_error(e, outputs={"status": "error", "query": user_query})
                answer = f"知识库中找到相关信息，但生成答案时出错：{str(e)}"
        else:
            # 如果没有LLM，直接返回检索到的知识
            answer = "以下是知识库中的相关信息：\n\n" + knowledge_context

        result = {
            "status": "success",
            "query": user_query,
            "answer": answer,
            "retrieved_documents": [
                {
                    "content": doc['content'][:200] + "..." if len(doc['content']) > 200 else doc['content'],
                    "metadata": doc['metadata'],
                    "retrieval_sources": doc.get("retrieval_sources", []),
                    "distance": doc.get("distance"),
                    "bm25_score": doc.get("bm25_score"),
                    "rrf_score": doc.get("rrf_score")
                }
                for doc in retrieved_docs
            ]
        }

        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

    def get_stats(self) -> Dict:
        """获取知识库统计信息"""
        if not self.initialized:
            return {"status": "error", "message": "Not initialized"}

        try:
            # 确保连接正常
            self._ensure_connection()
            stats = self.milvus_client.get_collection_stats(self.collection_name)
            return {
                "status": "success",
                "collection_name": self.collection_name,
                "total_documents": stats.get("row_count", 0),
                "knowledge_base_path": str(self.knowledge_base_path)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def close(self):
        """关闭 Milvus 连接"""
        if hasattr(self, 'milvus_client'):
            try:
                if hasattr(self.milvus_client, 'close'):
                    self.milvus_client.close()
                    logger.info("Milvus client closed successfully")
            except Exception as e:
                logger.warning(f"Error closing Milvus client: {e}")

    def __del__(self):
        """析构函数，确保资源被释放"""
        self.close()
