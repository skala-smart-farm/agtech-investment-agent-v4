"""언어 인식 하이브리드: 다른 언어 문서가 BM25 몫을 못 받아 밀리지 않는지 (impute), filter 는 같은 언어에만 BM25."""
from __future__ import annotations

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from rag.hybrid_lang import LanguageAwareHybrid, query_lang


class Fixed(BaseRetriever):
    docs: list[Document]

    def _get_relevant_documents(self, query, *, run_manager):
        return self.docs


def _d(i, lang):
    return Document(page_content=f"doc{i}", metadata={"doc_id": f"d{i}", "chunk_id": i, "page": 1, "lang": lang})


def test_query_lang():
    assert query_lang("스마트팜 시장 규모는?") == "ko" and query_lang("smart farm market size") == "en"


def test_impute_keeps_english_top_dense_doc_first():
    en1, ko2, ko3 = _d(1, "en"), _d(2, "ko"), _d(3, "ko")
    dense = Fixed(docs=[en1, ko2, ko3])            # 의미로는 영어 문서가 1위
    bm25 = Fixed(docs=[ko3, ko2])                  # 한국어 질문이라 BM25 는 한국어 문서만 잡는다
    q = "한국어 질문"
    imp = LanguageAwareHybrid(bm25=bm25, dense=dense, weights=[0.4, 0.6], mode="impute", k=3).invoke(q)
    flt = LanguageAwareHybrid(bm25=bm25, dense=dense, weights=[0.4, 0.6], mode="filter", k=3).invoke(q)
    assert imp[0].metadata["doc_id"] == "d1"       # 다른 언어 문서는 Dense 순위로 BM25 몫을 대신 받아 1위 유지
    assert flt[0].metadata["lang"] == "ko" and flt[-1].metadata["doc_id"] == "d1"  # filter 는 BM25 몫이 없어 영어 문서가 맨 뒤로 밀린다 (측정에서 효과 없던 이유)
