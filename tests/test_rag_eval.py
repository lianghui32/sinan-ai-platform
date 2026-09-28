"""RAG 评估框架回归：在完整合成语料上保证混合检索不弱于任何单路。

把评估脚本纳入 CI，防止后续改检索算法时指标静默回退（护栏测试）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from eval_rag import _fresh_kb, build_corpus, build_golden, evaluate  # noqa: E402


def test_hybrid_not_worse_than_any_single_retriever():
    docs = build_corpus()
    golden = build_golden()
    conn, kb_id = _fresh_kb(docs)
    try:
        m = evaluate(conn, kb_id, golden)
    finally:
        conn.close()
    # 头部精度与召回都不弱于单路（评估集上实测：hybrid 75%/100%/0.875）
    assert m["hybrid"]["hit@1"] >= m["tfidf"]["hit@1"]
    assert m["hybrid"]["hit@1"] >= m["bm25"]["hit@1"]
    assert m["hybrid"]["hit@3"] >= m["tfidf"]["hit@3"]
    assert m["hybrid"]["hit@3"] >= m["bm25"]["hit@3"]
    assert m["hybrid"]["hit@3"] >= 0.9
    assert m["hybrid"]["mrr"] >= m["tfidf"]["mrr"]
