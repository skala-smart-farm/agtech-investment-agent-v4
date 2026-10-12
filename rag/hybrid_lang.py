"""언어 인식 하이브리드 검색기: Kiwi BM25 + Dense 를 가중 RRF 로 합치되, 질문과 다른 언어의 문서에는 BM25 를 쓰지 않는다.

왜 필요한가: 에이전트의 질문은 한국어인데 코퍼스의 영어 문서(AgFunder · World Bank · 학술지 등)는 한국어 단어와 겹칠 일이 없다.
그래서 기존 하이브리드(rag.index.get_hybrid_retriever, EnsembleRetriever)에서는 영어 문서가 BM25 몫을 받지 못해 순위가 밀린다.
실측(outputs/eval/runtime_retriever.json): 영어 문서 27문항 MRR@4 가 Dense 만 0.892, 하이브리드 0.4:0.6 은 0.698.

mode
- "filter" : 질문과 같은 언어의 문서에만 BM25 점수를 더한다 (다른 언어 문서는 Dense 몫만)
- "impute" : 다른 언어 문서는 BM25 순위를 잴 수 없으므로 Dense 순위를 BM25 순위로 대신 쓴다 — 두 몫을 다 받는
             같은 언어 문서와 같은 조건에서 비교된다

rag/index.py 는 캐시 동결 파일이라 손대지 않고 여기에 따로 둔다.

결론(2026-10-12): 런타임에는 쓰지 않는다. 실제 에이전트 질의 63개 평가(eval/eval_runtime_queries.py, 판정 규칙 사전 고정)에서
런타임 하이브리드가 Precision@4 0.627 로 1위였고 impute 는 0.52~0.53 이었다 — 실제 질의는 대부분 한국어 문서를 찾고(53개 중 44개),
impute 가 대신 넣는 영어 조각이 대부분 비관련이었다. 영어 문서만 다루는 질의(9개)에서만 impute 가 근소하게 앞섰다.
기존 70문항(영어 정답 39%)에서의 우위는 문항 구성 때문이었다 → 평가 세트가 실제 질의 분포를 닮아야 한다는 교훈.
"""
from __future__ import annotations

import re

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

HANGUL = re.compile(r"[가-힣]")
RRF_C = 60  # EnsembleRetriever 와 같은 상수


def query_lang(q: str) -> str:
    return "ko" if HANGUL.search(q or "") else "en"


def _key(d: Document) -> tuple:
    m = d.metadata or {}
    return (m.get("doc_id"), m.get("chunk_id"), m.get("page")) if m.get("doc_id") else (d.page_content,)


class LanguageAwareHybrid(BaseRetriever):
    bm25: BaseRetriever
    dense: BaseRetriever
    weights: list[float]          # [BM25, Dense]
    mode: str = "impute"          # "filter" | "impute"
    k: int = 8

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        ql = query_lang(query)
        wb, wd = (float(x) for x in self.weights)
        b_docs, d_docs = self.bm25.invoke(query), self.dense.invoke(query)
        b_rank = {_key(d): r for r, d in enumerate(b_docs, 1)}
        d_rank = {_key(d): r for r, d in enumerate(d_docs, 1)}
        docs = {_key(d): d for d in [*b_docs, *d_docs]}
        score: dict[tuple, float] = {}
        for k, doc in docs.items():
            same = (doc.metadata or {}).get("lang", ql) == ql
            s = wd / (d_rank[k] + RRF_C) if k in d_rank else 0.0
            if same and k in b_rank:
                s += wb / (b_rank[k] + RRF_C)
            elif not same and self.mode == "impute" and k in d_rank:
                s += wb / (d_rank[k] + RRF_C)   # 다른 언어: 단어 일치를 잴 수 없어 Dense 순위로 대신한다
            score[k] = s
        order = sorted(docs, key=lambda k: -score[k])
        return [docs[k] for k in order][: self.k]
