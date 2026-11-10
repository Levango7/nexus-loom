"""知识库：文档切块 → 向量化 → 持久化 → 语义检索。

常见做法是进程内 TF-IDF 词袋，既不持久化也不跨任务复用。本模块提供的
是**可累积的个人知识库**：你喂进去的资料留下来，以后每次检索都能命中。

存储：SQLite，向量以 float32 二进制存 BLOB —— 无 numpy 依赖。

检索：暴力余弦扫描。个人知识库规模（千级文档 / 万级切块）下，
暴力扫描是毫秒级，引入 FAISS/Milvus 是过度设计。

关键实现约束：
1. **切块要结构感知**：按 Markdown 标题切，并把标题路径作为上下文前缀
   写进切块内容 —— 否则"## 部署"下的那段话脱离标题后语义残缺
2. **每个切块记录产出它的 embedder**：换嵌入模型后旧向量不可比，
   必须明确报错或重建，不能静默给出垃圾结果
3. 线程本地连接 + WAL
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from . import embeddings as _emb
from .sqlite_util import enable_wal

DEFAULT_DB = "knowledge_base.db"
# 单块目标长度（字符）。过短则上下文不足，过长则检索精度下降
DEFAULT_CHUNK_CHARS = 700
DEFAULT_CHUNK_OVERLAP = 120
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


class _TrackableConnection(sqlite3.Connection):
    """支持弱引用的连接（sqlite3.Connection 原生不可弱引用，经 factory 子类补槽）。"""


# ─────────────────────────────── 切块 ───────────────────────────────

def chunk_markdown(text: str, max_chars: int = DEFAULT_CHUNK_CHARS,
                   overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[dict[str, str]]:
    """按 Markdown 标题结构切块，返回 [{heading_path, content}]。

    标题路径（"总标题 > 章节 > 子节"）会作为前缀写进 content：
    检索时切块自带上下文，不依赖调用方拼接。
    """
    if not text or not text.strip():
        return []
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正整数")
    overlap = max(0, min(overlap, max_chars // 2))

    lines = text.splitlines()
    has_heading = any(_RE_HEADING.match(ln) for ln in lines)

    # 1) 按标题切段，记录标题栈构成路径
    sections: list[tuple[str, str]] = []     # (heading_path, body)
    stack: list[tuple[int, str]] = []        # (level, title)
    current_path = ""
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append((current_path, body))
        buf.clear()

    for line in lines:
        m = _RE_HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_path = " > ".join(t for _, t in stack)
        else:
            buf.append(line)
    flush()

    # 全文没有任何标题时才退化为"整篇一个切块"。
    # 注意不能写成 `if not sections`：标题存在但都没正文时 sections 也为空，
    # 那样会把标题行本身当成正文塞进知识库。
    if not sections and not has_heading:
        sections = [("", text.strip())]

    # 2) 超长段落切窗口（带重叠，避免切断语义）
    chunks: list[dict[str, str]] = []
    for path, body in sections:
        prefix = f"{path}\n" if path else ""
        if len(body) <= max_chars:
            chunks.append({"heading_path": path, "content": prefix + body})
            continue
        start = 0
        while start < len(body):
            piece = body[start:start + max_chars]
            chunks.append({"heading_path": path, "content": prefix + piece})
            if start + max_chars >= len(body):
                break
            start += max_chars - overlap

    return chunks


def _content_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=12).hexdigest()


# ───────────────────────────── 知识库 ─────────────────────────────

class KnowledgeBase:
    """SQLite 持久化的向量知识库。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB,
                 embedder: _emb.Embedder | None = None,
                 embedder_name: str = "hash", dim: int | None = None):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn_refs: set = set()
        self._write_lock = threading.Lock()
        self._embedder = embedder or _emb.get_embedder(
            embedder_name, **({"dim": dim} if dim else {}))
        self._init_db()

    # ── 连接管理（线程本地连接 + 自愈）──

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
            except sqlite3.Error:
                conn = None
        if conn is None:
            conn = sqlite3.connect(
                self.db_path, timeout=10, check_same_thread=False,
                factory=_TrackableConnection,
            )
            # WAL 切换需短暂独占锁、不走 busy handler：两进程首建同库时裸
            # PRAGMA 会直接 SQLITE_BUSY（与 timeout 无关）。走带退避重试的
            # enable_wal（与 db_backend 同款口径）；实现已下沉到本层
            # artesian/sqlite_util（分层门禁禁底座反向依赖主仓包），
            # 故此处顶层相对 import，无环。
            enable_wal(conn)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            self._conn_refs.add(weakref.ref(conn))
        return conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                doc_id       TEXT PRIMARY KEY,
                source       TEXT DEFAULT '',
                title        TEXT DEFAULT '',
                meta_json    TEXT DEFAULT '{}',
                content      TEXT DEFAULT '',
                chars        INTEGER DEFAULT 0,
                created_at   REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id     TEXT PRIMARY KEY,
                doc_id       TEXT NOT NULL,
                ordinal      INTEGER NOT NULL,
                heading_path TEXT DEFAULT '',
                content      TEXT NOT NULL,
                embedder     TEXT NOT NULL,
                dim          INTEGER NOT NULL,
                vector       BLOB NOT NULL,
                created_at   REAL NOT NULL,
                FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
            CREATE INDEX IF NOT EXISTS idx_docs_source ON documents(source);
        """)
        conn.commit()

    def close_all(self) -> None:
        for ref in list(self._conn_refs):
            conn = ref()
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
        self._conn_refs.clear()
        self._local = threading.local()

    # ── 写入 ──

    def add_document(self, content: str, source: str = "", title: str = "",
                     doc_id: str = "", meta: dict | None = None,
                     replace: bool = True) -> dict[str, Any]:
        """把一份文档切块、向量化并入库。

        replace=True 且 source 已存在时，先替换旧版本 —— 资料更新后
        不会残留过期切块污染检索结果。
        """
        if not content or not content.strip():
            return {"status": "error", "message": "内容为空"}

        with self._write_lock:
            conn = self._get_conn()

            if not doc_id:
                # 默认按来源+内容定 id：同源不同内容算不同文档
                doc_id = "d_" + _content_hash(f"{source}\x00{content}")

            replaced = 0
            if replace and source:
                stale = [r[0] for r in conn.execute(
                    "SELECT doc_id FROM documents WHERE source = ? AND doc_id != ?",
                    (source, doc_id)).fetchall()]
                for old in stale:
                    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (old,))
                    conn.execute("DELETE FROM documents WHERE doc_id = ?", (old,))
                replaced = len(stale)

            chunks = chunk_markdown(content)
            if not chunks:
                return {"status": "error", "message": "切块后无有效内容"}

            vectors = self._embedder.embed([c["content"] for c in chunks])
            if len(vectors) != len(chunks):
                return {"status": "error",
                        "message": f"嵌入数量不匹配: {len(vectors)} != {len(chunks)}"}

            now = time.time()
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute(
                "INSERT OR REPLACE INTO documents"
                " (doc_id, source, title, meta_json, content, chars, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (doc_id, source, title,
                 json.dumps(meta or {}, ensure_ascii=False),
                 content, len(content), now))

            for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
                conn.execute(
                    "INSERT OR REPLACE INTO chunks"
                    " (chunk_id, doc_id, ordinal, heading_path, content,"
                    "  embedder, dim, vector, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"{doc_id}:{i}", doc_id, i, chunk["heading_path"],
                     chunk["content"], self._embedder.name, len(vec),
                     _emb.pack_vector(vec), now))
            conn.commit()

        return {"status": "ok", "doc_id": doc_id, "chunks": len(chunks),
                "chars": len(content), "replaced": replaced,
                "embedder": self._embedder.name}

    def add_file(self, path: str | Path, **kwargs) -> dict[str, Any]:
        """读文件入库（UTF-8 / GBK 嗅探）。"""
        p = Path(path)
        if not p.exists():
            return {"status": "error", "message": f"文件不存在: {p}"}
        raw = p.read_bytes()
        text = ""
        for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        kwargs.setdefault("source", str(p))
        kwargs.setdefault("title", p.stem)
        return self.add_document(text, **kwargs)

    # ── 检索 ──

    def search(self, query: str, top_k: int = 5, min_score: float = 0.0,
               max_per_doc: int = 0, source_prefix: str = "") -> dict[str, Any]:
        """向量检索。返回 top_k 个最相关切块。

        max_per_doc > 0 时限每份文档最多贡献几个切块 —— 避免一份长文档
        霸占全部结果位（结果多样性）。
        """
        if not query or not query.strip():
            return {"status": "error", "message": "查询为空", "results": []}

        conn = self._get_conn()
        sql = ("SELECT c.chunk_id, c.doc_id, c.ordinal, c.heading_path,"
               " c.content, c.embedder, c.dim, c.vector,"
               " d.source, d.title"
               " FROM chunks c JOIN documents d ON d.doc_id = c.doc_id")
        params: list[Any] = []
        if source_prefix:
            sql += " WHERE d.source LIKE ?"
            params.append(f"{source_prefix}%")

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return {"status": "ok", "results": [], "scanned": 0,
                    "embedder": self._embedder.name}

        qv = self._embedder.embed_one(query)

        # 向量空间不一致的切块必须排除：不同嵌入器（或同器不同维度）产出的
        # 向量不可比，强行比较只会给出无意义的分数。
        # 全部不可比时**明确报错**而不是静默返回空——后者会让调用方
        # 以为"知识库里没有相关内容"，实际是配置问题。
        by_name = [r for r in rows if r[5] != self._embedder.name]
        by_dim = [r for r in rows
                  if r[5] == self._embedder.name and r[6] != len(qv)]

        if rows and len(by_name) + len(by_dim) == len(rows):
            reasons = []
            if by_name:
                seen = sorted({r[5] for r in by_name})
                reasons.append(
                    f"{len(by_name)} 个切块由 {('、'.join(seen))} 产出")
            if by_dim:
                dims = sorted({r[6] for r in by_dim})
                reasons.append(
                    f"{len(by_dim)} 个切块维度为 {dims}")
            # 必须同时报出**库里存的是什么**与**当前用的是什么**：
            # 只说一边，用户不知道该换嵌入器还是该重建
            return {
                "status": "error", "results": [], "scanned": len(rows),
                "embedder": self._embedder.name,
                "stored_embedders": sorted({r[5] for r in rows}),
                "message": (
                    "知识库向量空间与当前嵌入器不一致："
                    + "；".join(reasons)
                    + f"；当前嵌入器为 {self._embedder.name}"
                    + "。请用相同嵌入器检索，或调用 rebuild() 重建为当前嵌入器。"
                ),
            }

        scored: list[tuple[float, tuple]] = []
        for r in rows:
            if r[5] != self._embedder.name or r[6] != len(qv):
                continue
            score = _emb.cosine(qv, _emb.unpack_vector(r[7]))
            if score >= min_score:
                scored.append((score, r))
        scored.sort(key=lambda kv: kv[0], reverse=True)

        results: list[dict[str, Any]] = []
        per_doc: dict[str, int] = {}
        for score, r in scored:
            if max_per_doc and per_doc.get(r[1], 0) >= max_per_doc:
                continue
            per_doc[r[1]] = per_doc.get(r[1], 0) + 1
            results.append({
                "chunk_id": r[0], "doc_id": r[1], "ordinal": r[2],
                "heading_path": r[3], "content": r[4],
                "source": r[8], "title": r[9], "score": round(score, 4),
            })
            if len(results) >= top_k:
                break

        return {"status": "ok", "results": results, "scanned": len(rows),
                "embedder": self._embedder.name,
                # 部分切块不可比时如实上报（如换模型后只重建了一部分），
                # 便于调用方发现"检索面不完整"
                "skipped_incompatible": len(by_name) + len(by_dim)}

    # ── 管理 ──

    def list_documents(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT d.doc_id, d.source, d.title, d.chars, d.created_at,"
            " (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.doc_id)"
            " FROM documents d ORDER BY d.created_at DESC").fetchall()
        return [{"doc_id": r[0], "source": r[1], "title": r[2],
                 "chars": r[3], "created_at": r[4], "chunks": r[5]}
                for r in rows]

    def delete_document(self, doc_id: str) -> bool:
        with self._write_lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            cur = conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            conn.commit()
            return cur.rowcount > 0

    def stats(self) -> dict[str, Any]:
        conn = self._get_conn()
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        chars = conn.execute(
            "SELECT COALESCE(SUM(chars), 0) FROM documents").fetchone()[0]
        embedders = [r[0] for r in conn.execute(
            "SELECT DISTINCT embedder FROM chunks").fetchall()]
        return {"documents": docs, "chunks": chunks, "chars": chars,
                "embedders": embedders, "current_embedder": self._embedder.name,
                "consistent": embedders in ([], [self._embedder.name])}

    def rebuild(self, embedder: _emb.Embedder | None = None) -> dict[str, Any]:
        """用当前（或指定）嵌入器重建全部向量。

        换嵌入模型后必须调用：旧向量与新查询向量不可比。
        """
        if embedder is not None:
            self._embedder = embedder
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT chunk_id, content FROM chunks ORDER BY doc_id, ordinal"
        ).fetchall()
        if not rows:
            return {"status": "ok", "rebuilt": 0, "embedder": self._embedder.name}

        vectors = self._embedder.embed([r[1] for r in rows])
        with self._write_lock:
            for (chunk_id, _), vec in zip(rows, vectors, strict=True):
                conn.execute(
                    "UPDATE chunks SET embedder=?, dim=?, vector=? WHERE chunk_id=?",
                    (self._embedder.name, len(vec), _emb.pack_vector(vec), chunk_id))
            conn.commit()
        return {"status": "ok", "rebuilt": len(rows),
                "embedder": self._embedder.name}
