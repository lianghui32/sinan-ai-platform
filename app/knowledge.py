"""企业知识库：文档解析(txt/md/docx/csv) → 分块 → 检索（TF-IDF 余弦 / BM25 / RRF 混合）。

向量化方案（轻量 RAG，无外部模型依赖）：
1. 分词：英文/数字按单词，中文按单字 + 相邻双字(bigram)，兼顾中英文混合文档；
2. 哈希向量化：每个词用 md5 哈希映射到固定 512 维桶，得到稀疏词频；
3. 检索模式（search_kb 的 mode 参数）：
   - tfidf  ：TF-IDF 余弦相似度。对词频分布敏感，长文档里常见词贡献被压低；
   - bm25   ：Okapi BM25。对"查询词是否精确出现"敏感，专有名词/型号类查询更准；
   - hybrid ：两路检索各出一份排名，用 Reciprocal Rank Fusion (RRF, k=60) 融合，
     对单一排序器的偏差更鲁棒（scripts/eval_rag.py 用 golden set 实测对比三种模式）。
"""
import hashlib
import io
import json
import math
import re
from collections import Counter

import numpy as np

DIM = 512                 # 哈希向量维度
CHUNK_SIZE = 400          # 每块目标字符数
CHUNK_OVERLAP = 60        # 块间重叠字符数

_EN_WORD = re.compile(r"[a-zA-Z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------- 文档解析
def parse_upload(filename: str, raw: bytes) -> str:
    """按扩展名解析上传文件为纯文本。支持 txt/md/csv/docx。"""
    lower = filename.lower()
    if lower.endswith(".docx"):
        return _parse_docx(raw)
    if lower.endswith((".txt", ".md", ".markdown", ".csv", ".log", ".json")):
        for enc in ("utf-8", "utf-8-sig", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    raise ValueError(f"不支持的文件类型: {filename}（仅支持 txt/md/docx/csv）")


def _parse_docx(raw: bytes) -> str:
    import zipfile

    # zip 炸弹防护：2MB 的 docx 解压后可能膨胀成 GB 级 document.xml，全量读进内存会拖死进程。
    # 先按 zip 中央目录登记的解压后大小设上限（登记值可伪造，但足够拦截常见炸弹包）。
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            infos = zf.infolist()
            total_unc = sum(i.file_size for i in infos)
            if len(infos) > 500 or total_unc > 50 * 1024 * 1024:
                raise ValueError(f"docx 解压后体积异常（{total_unc // 1024 // 1024}MB），疑似 zip 炸弹，已拒绝")
    except zipfile.BadZipFile as exc:
        raise ValueError("不是合法的 docx 文件") from exc

    from docx import Document  # python-docx

    doc = Document(io.BytesIO(raw))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:  # 表格按行拼接
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append("，".join(cells))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 分块
def split_chunks(text: str) -> list[str]:
    """按段落聚合 + 长段滑窗切分，带重叠，保证上下文连续。"""
    text = re.sub(r"\r\n?", "\n", text or "")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        while len(para) > CHUNK_SIZE:  # 超长段落按句号/字符滑窗切开
            if buf:
                chunks.append(buf)
                buf = ""
            cut = CHUNK_SIZE
            for sep in ("。", "！", "？", "；", ". ", "\n"):
                idx = para.rfind(sep, CHUNK_SIZE // 2, CHUNK_SIZE)
                if idx > 0:
                    cut = idx + len(sep)
                    break
            chunks.append(para[:cut])
            para = para[max(cut - CHUNK_OVERLAP, 1):]
        if len(buf) + len(para) + 2 <= CHUNK_SIZE:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            buf = para
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


# ---------------------------------------------------------------- 向量化
def tokenize(text: str) -> list[str]:
    """英文单词 + 中文单字 + 中文相邻双字(bigram)。"""
    text = text.lower()
    tokens = _EN_WORD.findall(text)
    cjk_chars = _CJK.findall(text)
    tokens.extend(cjk_chars)
    tokens.extend(a + b for a, b in zip(cjk_chars, cjk_chars[1:]))
    return tokens


def _bucket(term: str) -> int:
    return int(hashlib.md5(term.encode("utf-8")).hexdigest(), 16) % DIM


def term_counts(text: str) -> dict[int, int]:
    """稀疏哈希词频：{桶下标: 次数}。"""
    counter: Counter = Counter()
    for t in tokenize(text):
        counter[_bucket(t)] += 1
    return dict(counter)


# ---------------------------------------------------------------- 入库
def ingest_text(conn, kb_id: int, filename: str, text: str) -> dict:
    """把一段文本作为文档写入知识库（分块 + 建向量索引）。返回文档信息。"""
    from .db import now

    chunks = split_chunks(text)
    cur = conn.execute(
        "INSERT INTO documents(kb_id,filename,char_count,chunk_count,created_at) VALUES(?,?,?,?,?)",
        (kb_id, filename, len(text), len(chunks), now()),
    )
    doc_id = cur.lastrowid
    for seq, chunk_text in enumerate(chunks):
        conn.execute(
            "INSERT INTO chunks(doc_id,kb_id,seq,text,counts_json) VALUES(?,?,?,?,?)",
            (doc_id, kb_id, seq, chunk_text, json.dumps(term_counts(chunk_text))),
        )
    conn.commit()
    return {"doc_id": doc_id, "chunk_count": len(chunks), "char_count": len(text)}


def delete_document(conn, doc_id: int) -> None:
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.commit()


# ---------------------------------------------------------------- 检索
def _load_kb_rows(conn, kb_id: int) -> list:
    return conn.execute(
        "SELECT c.id,c.doc_id,c.seq,c.text,c.counts_json,d.filename "
        "FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.kb_id = ?",
        (kb_id,),
    ).fetchall()


def _tfidf_scores(rows: list, query: str) -> np.ndarray:
    """TF-IDF 哈希向量余弦相似度（原检索算法，保持不变）。"""
    n = len(rows)
    q_counts = term_counts(query)
    if not q_counts:
        return np.zeros(n)
    mat = np.zeros((n, DIM), dtype=np.float64)
    for i, r in enumerate(rows):
        for idx_s, cnt in json.loads(r["counts_json"]).items():
            mat[i, int(idx_s)] = cnt

    df = (mat > 0).sum(axis=0)                       # 每个桶的文档频率
    idf = np.log((1.0 + n) / (1.0 + df)) + 1.0       # 平滑 IDF

    mat = mat * idf                                   # TF-IDF
    q = np.zeros(DIM, dtype=np.float64)
    for idx, cnt in q_counts.items():
        q[idx] = cnt * idf[idx]

    mat_norm = np.linalg.norm(mat, axis=1)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return np.zeros(n)
    return np.where(mat_norm > 0, mat @ q / (mat_norm * q_norm + 1e-12), 0.0)


def _bm25_scores(rows: list, query: str) -> np.ndarray:
    """Okapi BM25（k1=1.5, b=0.75）。BM25 以"查询词是否精确命中"为核心，
    对产品型号、专有名词这类低频强区分词比余弦相似度更敏感。"""
    from collections import Counter

    n = len(rows)
    doc_tokens = [tokenize(r["text"]) for r in rows]
    q_tokens = tokenize(query)
    if not q_tokens or n == 0:
        return np.zeros(n)
    avgdl = sum(len(t) for t in doc_tokens) / n or 1.0
    df: Counter = Counter()
    for toks in doc_tokens:
        df.update(set(toks))
    k1, b = 1.5, 0.75
    scores = np.zeros(n, dtype=np.float64)
    for i, toks in enumerate(doc_tokens):
        tf = Counter(toks)
        dl = len(toks) or 1
        for qt in q_tokens:
            f = tf.get(qt, 0)
            if not f:
                continue
            idf = math.log(1.0 + (n - df[qt] + 0.5) / (df[qt] + 0.5))
            scores[i] += idf * (f * (k1 + 1)) / (f + k1 * (1.0 - b + b * dl / avgdl))
    return scores


def _rrf_fuse(scores_a: np.ndarray, scores_b: np.ndarray, k: int = 60,
              w_a: float = 0.3, w_b: float = 0.7) -> np.ndarray:
    """Reciprocal Rank Fusion（加权版）：Σ w / (k+rank)。

    用排名而不是原始分数融合，回避了"余弦分与 BM25 分值纲不同、无法直接加权"的问题；
    单路无命中的文档不参与该路融合，不会被另一路的噪声抬升。
    权重由 scripts/eval_rag.py 的 golden set 实测选定：等权 0.5/0.5 会用 TF-IDF 的噪声
    稀释 BM25 的头部精度（hit@1 75%→62.5%），向 BM25 倾斜到 0.7 后与最优单路持平
    （hit@1 75% / hit@3 100%），同时保留 TF-IDF 在精确词失配语料上的第二路兜底。
    k 取 RRF 论文惯用的 60（k 敏感性实验见 eval/report.md）。
    """
    n = len(scores_a)
    fused = np.zeros(n, dtype=np.float64)
    for scores, w in ((scores_a, w_a), (scores_b, w_b)):
        order = np.argsort(-scores)
        rank = np.empty(n, dtype=np.float64)
        rank[order] = np.arange(1, n + 1)
        valid = scores > 0
        fused[valid] += w / (k + rank[valid])
    return fused


def search_kb(conn, kb_id: int, query: str, top_k: int = 3, mode: str = "hybrid") -> list[dict]:
    """知识库检索，返回 [{doc_id, filename, seq, text, score}]。

    mode: tfidf（TF-IDF 余弦）/ bm25（Okapi BM25）/ hybrid（RRF 融合，默认）。
    """
    if mode not in ("tfidf", "bm25", "hybrid"):
        raise ValueError(f"未知检索模式: {mode}")
    rows = _load_kb_rows(conn, kb_id)
    if not rows or not query.strip():
        return []

    if mode == "tfidf":
        scores = _tfidf_scores(rows, query)
    elif mode == "bm25":
        scores = _bm25_scores(rows, query)
    else:
        scores = _rrf_fuse(_tfidf_scores(rows, query), _bm25_scores(rows, query))

    order = np.argsort(-scores)[:top_k]
    results = []
    for i in order:
        if scores[i] <= 0.01:
            continue
        r = rows[int(i)]
        results.append({
            "doc_id": r["doc_id"],
            "filename": r["filename"],
            "seq": r["seq"],
            "text": r["text"],
            "score": round(float(scores[i]), 4),
            "mode": mode,
        })
    return results


def build_context(refs: list[dict], max_chars: int = 1600) -> str:
    """把检索结果拼成注入 system prompt 的上下文段落。"""
    parts = []
    used = 0
    for i, r in enumerate(refs, 1):
        snippet = r["text"]
        if used + len(snippet) > max_chars:
            snippet = snippet[: max(0, max_chars - used)]
        parts.append(f"[资料{i}] 来源《{r['filename']}》第{r['seq'] + 1}段：\n{snippet}")
        used += len(snippet)
        if used >= max_chars:
            break
    return "\n\n".join(parts)
