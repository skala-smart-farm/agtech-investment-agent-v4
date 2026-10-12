"""실제 에이전트 질의로 검색기 평가: 앞 4개 조각의 Precision@4 · Hit@4 (LLM Judge 관련성 O/X).

왜: 기존 검색 평가 70문항(eval_retrieval · eval_final_retriever · eval_lang_hybrid)은 문서를 보고 만든 질문이라
에이전트가 실제로 던지는 질의(키워드 나열, 재작성된 긴 질의, 영어 질의)와 다를 수 있다. 여기서는 본 실행
(outputs/run_log.json)과 보정 실행(outputs/calibration/run_log.json)의 rag_traces 에 남은 실제 질의에 검색기 넷을
적용하고, 앞 4개 조각이 질의에 관련 있는지 LLM Judge 가 이진(O/X)으로 판정한다. 정답 조각을 미리 정하지 않으므로
Recall·MRR(정답 기준) 대신 Precision@4 · Hit@4 를 잰다.

질의 수집 (같은 문자열은 앞뒤 공백을 지우고 한 번만)
- 문서 질의: search_documents 에 실제로 들어간 질의 — grade 단계(v1 경로는 단계 이름 없는 기록)의 query, 재작성 질의 포함
- 웹 질의: agent 단계의 web_query, web 단계의 query. 웹 경로로 간 질의도 문서 검색을 적용해 잰다(문서 검색 평가)

검색기 (모두 후보 k = rag.candidate_k, 합친 순서의 앞 rag.top_k 개만 판정 — 런타임 Judge 가 보는 순서 그대로)
- A runtime          : rag.index.get_hybrid_retriever() — Kiwi BM25 + KURE-v1 Dense, EnsembleRetriever 가중 RRF
- B dense only       : 같은 FAISS 색인의 Dense 만 (= ensemble_weights [0, 1] 과 앞 k개 순서가 같다)
- C impute 0.4:0.6 / 0.5:0.5 : rag.hybrid_lang.LanguageAwareHybrid(mode="impute")

Judge: config models.judge(gpt-4.1-mini), temperature 0, 질의 하나와 조각 최대 4개를 한 호출로 묶는다.
같은 (질의, 조각) 쌍은 한 번만 판정한다: 질의마다 네 검색기 앞 4개의 합집합을 모아 판정이 없는 조각만 4개씩 묶는다.
그래서 같은 조각은 어느 검색기에서 나와도 같은 판정을 받는다. 판정은 outputs/eval/runtime_queries_judgments.json 에
쌓이고(재현용 겸 캐시) 다시 실행하면 거기서 읽는다. replay/llm_cache.sqlite(제출본 재현용 캐시)는 건드리지 않는다.

지표 (질의마다 계산해 평균)
- Precision@4 = 앞 4개 중 관련 조각 수 / 4,  Hit@4 = 앞 4개에 관련 조각이 하나라도 있으면 1
- 참고(판정 규칙에 쓰지 않음): MRR@4(첫 관련 조각 순위의 역수), 앞 4개 중 영어 조각 비율
묶음: 전체 / 질의가 다루는 문서 언어(어느 검색기에서든 관련 판정을 받은 조각의 lang: ko 만 · en 만 · 둘 다 ·
관련 조각 없음) / 질의 종류(문서 · 웹) / 질의 언어(ko · en)

판정 규칙 (측정 전에 고정, 코드가 기계적으로 적용):
  1) 실제 질의 전체 Precision@4 평균이 가장 높은 검색기
  2) 소수 셋째 자리까지 같으면 전체 Hit@4 가 높은 검색기
  3) 그래도 같으면 지금 런타임(A), 그다음 표의 앞쪽
보조(판정에 쓰지 않음): A 대비 질의별 Precision@4 차이의 승·패·동 수, 짝지은 부트스트랩 95% 구간(2000회, seed 0),
  한쪽 앞 4개에만 있는 조각의 언어·관련 판정 분해, 본 실행 런타임 Judge(run_log 의 kept_ranks)와의 판정 일치도

비용: 호출 전에 tiktoken(o200k_base)으로 프롬프트 토큰을 세어 예상 비용을 출력·기록한다. 예산(--budget, 기본 $0.20)을
넘으면 수집 순서 뒤쪽 질의부터 뺀다. 실제 비용은 응답의 토큰 사용량(core.cost.TRACKER)으로 잰다.

    python -m eval.eval_runtime_queries --env <.env 경로>   # 판정 없는 쌍만 API 호출
    python -m eval.eval_runtime_queries --dry-run           # 검색 · 예상 비용만 (API 호출 없음)
    python -m eval.eval_runtime_queries --offline           # 저장된 판정만으로 다시 계산 (없는 쌍이 있으면 실패)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from core.config import get_config, path

RUN_LOGS = ("outputs/run_log.json", "outputs/calibration/run_log.json")
OUT_JSON, OUT_MD = "outputs/eval/runtime_queries.json", "outputs/eval/runtime_queries.md"
JUDGMENTS = "outputs/eval/runtime_queries_judgments.json"
BATCH = 4                      # 한 호출에 묶는 조각 수
MAX_CHARS = 1200               # 조각 본문 최대 글자 수 (= config rag.context_doc_chars, 답변 생성이 보는 길이)
BUDGET_USD = 0.20
EST_OUT_PER_ITEM, EST_OUT_BASE, EST_IN_OVERHEAD, EST_MARGIN = 15, 10, 120, 1.10   # 예상 비용 보정치(토큰)
RULE = ("1) 실제 질의 전체 Precision@4 평균 최대 2) 같으면(소수 셋째 자리) 전체 Hit@4 최대 "
        "3) 그래도 같으면 지금 런타임(A), 그다음 표의 앞쪽")
DOC_LANG_LABEL = {"ko": "한국어 문서만", "en": "영어 문서만", "mixed": "한국어·영어 둘 다", "none": "관련 조각 없음"}

PROMPT = """너는 투자 분석팀의 검색 결과 평가자다. 아래 검색 질의에 대해 각 문서 조각을 판정하라.
질의가 찾는 정보(수치, 사실, 정책 내용 등)를 조각이 직접 담고 있어 질의에 답하는 데 쓸 수 있으면 relevant=true,
주제(분야)만 비슷하고 질의가 찾는 내용이 없으면 false, 목차·표지·참고문헌 목록도 false 다. 애매하면 false.
조각의 언어(한국어·영어)는 판정에 영향을 주지 않는다.

검색 질의: {query}

조각 목록:
{chunks}

모든 조각 번호에 대해 판정을 반환하라."""


class Judgment(BaseModel):
    idx: int = Field(description="조각 번호")
    relevant: bool = Field(description="질의에 답하는 데 직접 쓸 수 있는 정보가 있으면 true")


class Grades(BaseModel):
    judgments: list[Judgment]


# ── 순수 함수 (테스트 대상, LLM·검색 없음) ─────────────────────────────────────
def collect_queries(logs: list[tuple[str, dict]]) -> list[dict]:
    """run_log 들의 rag_traces 에서 실제 질의를 모아 중복을 지운다 (첫 등장 순서).
    kind: 문서 검색에 쓰인 적이 있으면 'doc', 웹 검색에만 쓰였으면 'web'."""
    seen: dict[str, dict] = {}
    for src, log in logs:
        for t in log.get("rag_traces") or []:
            for s in t.get("trace") or []:
                step = s.get("step")
                found = []
                if step in (None, "grade") and s.get("query"):
                    found.append(("doc", s["query"]))
                if step == "agent" and s.get("web_query"):
                    found.append(("web", s["web_query"]))
                if step == "web" and s.get("query"):
                    found.append(("web", s["query"]))
                for kind, q in found:
                    q = q.strip()
                    if not q:
                        continue
                    row = seen.setdefault(q, {"query": q, "kinds": [], "agent": t.get("agent"),
                                              "sub_question": t.get("question"), "sources": []})
                    if kind not in row["kinds"]:
                        row["kinds"].append(kind)
                    if src not in row["sources"]:
                        row["sources"].append(src)
    out = []
    for i, row in enumerate(seen.values(), 1):
        row["kind"] = "doc" if "doc" in row["kinds"] else "web"
        out.append({"qid": f"q{i:02d}", **row})
    return out


def precision_at_k(flags: list[bool], k: int) -> float:
    return sum(bool(f) for f in flags[:k]) / k


def hit_at_k(flags: list[bool], k: int) -> float:
    return float(any(flags[:k]))


def rr_at_k(flags: list[bool], k: int) -> float:
    return next((1 / (i + 1) for i, f in enumerate(flags[:k]) if f), 0.0)


def doc_lang(langs: set[str]) -> str:
    """질의가 다루는 문서 언어: 관련 판정을 받은 조각의 lang 집합 → ko · en · mixed · none."""
    langs = {x for x in langs if x}
    if not langs:
        return "none"
    return "mixed" if len(langs) > 1 else next(iter(langs))


def judge_order(ranked: dict[str, list[str]]) -> list[str]:
    """검색기별 앞 k개(조각 키 목록)의 합집합. 순서: 가장 좋은 순위 → 표 순서(검색기 이름 순서) → 첫 등장."""
    best: dict[str, tuple[int, int]] = {}
    for j, keys in enumerate(ranked.values()):
        for r, key in enumerate(keys):
            best[key] = min(best.get(key, (r, j)), (r, j))
    return sorted(best, key=lambda x: best[x])


def plan_calls(queries: list[dict], ranked: dict[str, dict[str, list[str]]], judged: set[tuple[str, str]],
               size: int = BATCH) -> list[tuple[str, list[str]]]:
    """판정이 없는 (질의, 조각) 쌍만 질의마다 size 개씩 묶는다. ranked = {검색기: {qid: [조각 키]}}."""
    calls = []
    for q in queries:
        union = judge_order({name: r[q["qid"]] for name, r in ranked.items()})
        todo = [key for key in union if (q["query"], key) not in judged]
        calls += [(q["qid"], todo[i:i + size]) for i in range(0, len(todo), size)]
    return calls


def summarize(flags_by_q: dict[str, list[bool]], langs_by_q: dict[str, list[str]], qids: list[str], k: int) -> dict:
    """질의별 플래그 → 평균 지표. flags_by_q[qid] 는 검색 순서대로의 관련 여부(앞 k개)."""
    if not qids:
        return {"n": 0}
    n = len(qids)
    en = [sum(x == "en" for x in langs_by_q[q][:k]) / k for q in qids]
    return {"n": n,
            "P@4": round(sum(precision_at_k(flags_by_q[q], k) for q in qids) / n, 3),
            "Hit@4": round(sum(hit_at_k(flags_by_q[q], k) for q in qids) / n, 3),
            "MRR@4": round(sum(rr_at_k(flags_by_q[q], k) for q in qids) / n, 3),
            "en_share": round(sum(en) / n, 3)}


def pick(rows: list[dict], runtime: str) -> dict:
    """판정 규칙: 전체 P@4 → 전체 Hit@4 (소수 셋째 자리) → 지금 런타임 → 표의 앞쪽."""
    def key(t):
        i, r = t
        return r["all"]["P@4"], r["all"]["Hit@4"], r["name"] == runtime, -i
    return max(enumerate(rows), key=key)[1]


def paired(a: list[float], b: list[float], n: int = 2000, seed: int = 0) -> dict:
    """b − a 의 질의별 차이: 승(b>a)·패·동, 평균 차이와 짝지은 부트스트랩 95% 구간 (판정 규칙에는 쓰지 않는 참고값)."""
    d = [y - x for x, y in zip(a, b)]
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(d) for _ in d) / len(d) for _ in range(n))
    return {"win": sum(x > 0 for x in d), "loss": sum(x < 0 for x in d), "tie": sum(x == 0 for x in d),
            "mean_diff": round(sum(d) / len(d), 3),
            "ci95": [round(means[int(0.025 * n)], 3), round(means[int(0.975 * n) - 1], 3)]}


def diff_breakdown(base: dict[str, list[dict]], other: dict[str, list[dict]]) -> dict:
    """질의마다 한쪽 앞 k개에만 있는 조각을 (언어, 관련 여부)로 센다. 값은 {"chunk", "lang", "relevant"} 목록.
    두 검색기의 Precision@4 차이는 '다른 쪽에만 있는 관련 조각 수' 차이에서 나온다 (참고값)."""
    out = {side: {f"{lang}_{rel}": 0 for lang in ("ko", "en") for rel in ("rel", "not")} for side in ("base", "other")}
    for qid in base:
        a, b = {x["chunk"]: x for x in base[qid]}, {x["chunk"]: x for x in other[qid]}
        for side, mine, theirs in (("base", a, b), ("other", b, a)):
            for key, x in mine.items():
                if key not in theirs:
                    out[side][f"{x['lang']}_{'rel' if x['relevant'] else 'not'}"] += 1
    return out


def runtime_agreement(logs: list[tuple[str, dict]], flags_by_query: dict[str, list[bool]], k: int) -> dict:
    """Judge 점검(참고): 본 실행의 런타임 Judge(prompts/rag_grade.md, 하위 질문 기준, 후보 8개를 한 번에 판정)와
    이 평가 Judge(질의 기준)의 런타임 검색기 앞 k개 판정 일치도. 각 trace 의 첫 grade 단계 중 dropped == 0 인 것만 쓴다
    (그때 kept_ranks = 런타임 Judge 가 관련으로 본 순위 전체). 같은 질의는 처음 한 번만. 본 실행의 검색 순서가 지금
    런타임 검색기와 같다고 가정한다(같은 코퍼스 해시 · 같은 설정)."""
    c = {"both": 0, "ours_only": 0, "runtime_only": 0, "neither": 0}
    seen = set()
    for _, log in logs:
        for t in log.get("rag_traces") or []:
            g = next((s for s in t.get("trace") or [] if s.get("step") in (None, "grade") and s.get("query")), None)
            if not g or g.get("dropped") != 0 or "kept_ranks" not in g:
                continue
            q = g["query"].strip()
            if q in seen or q not in flags_by_query:
                continue
            seen.add(q)
            for i, ours in enumerate(flags_by_query[q][:k]):
                theirs = (i + 1) in g["kept_ranks"]
                c["both" if ours and theirs else "ours_only" if ours else "runtime_only" if theirs else "neither"] += 1
    n = sum(c.values())
    if not n:
        return {"queries": 0, **c}
    po = (c["both"] + c["neither"]) / n
    p1, p2 = (c["both"] + c["ours_only"]) / n, (c["both"] + c["runtime_only"]) / n
    pe = p1 * p2 + (1 - p1) * (1 - p2)
    return {"queries": len(seen), "pairs": n, **c, "agreement": round(po, 3),
            "kappa": round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0}


def fit_budget(cost_by_qid: dict[str, float], order: list[str], budget: float) -> list[str]:
    """수집 순서 앞에서부터 예상 비용 합이 예산 안에 드는 질의까지 남긴다."""
    kept, total = [], 0.0
    for qid in order:
        c = cost_by_qid.get(qid, 0.0)
        if total + c > budget:
            break
        kept.append(qid)
        total += c
    return kept


# ── 검색 · 판정 ─────────────────────────────────────────────────────────────
def chunk_key(meta: dict) -> str:
    return f"{meta['doc_id']}#{meta['chunk_id']}"


def listing(chunks: list[dict]) -> str:
    return "\n\n".join(f"[{i}] ({c['publisher']} {c['year']}, p.{c['page']})\n{c['text'][:MAX_CHARS]}"
                       for i, c in enumerate(chunks))


def prompt_sha() -> str:
    return hashlib.sha256((PROMPT + str(MAX_CHARS) + json.dumps(Grades.model_json_schema())).encode()).hexdigest()[:12]


def retrievers(k: int) -> dict:
    from rag.hybrid_lang import LanguageAwareHybrid
    from rag.index import get_bm25, get_hybrid_retriever, get_vectorstore

    bm25 = get_bm25(k)
    dense = get_vectorstore().as_retriever(search_kwargs={"k": k})  # 런타임 설정 임베딩(KURE-v1)·같은 FAISS 색인
    w = [float(x) for x in get_config().rag.ensemble_weights]
    return {f"A runtime hybrid {w[0]}:{w[1]}": get_hybrid_retriever(),
            "B dense only": dense,
            "C lang-impute 0.4:0.6": LanguageAwareHybrid(bm25=bm25, dense=dense, weights=[0.4, 0.6], mode="impute", k=k),
            "C lang-impute 0.5:0.5": LanguageAwareHybrid(bm25=bm25, dense=dense, weights=[0.5, 0.5], mode="impute", k=k)}


def load_judgments(corpus: str) -> dict:
    f = path(JUDGMENTS)
    base = {"corpus_hash": corpus, "prompt_sha": prompt_sha(), "model": get_config().models.judge,
            "temperature": get_config().models.temperature, "max_chars": MAX_CHARS, "calls": [], "judgments": []}
    if not f.exists():
        return base
    data = json.loads(f.read_text(encoding="utf-8"))
    for k in ("corpus_hash", "prompt_sha", "model"):
        if data.get(k) != base[k]:
            sys.exit(f"{JUDGMENTS} 의 {k}={data.get(k)} 가 지금({base[k]})과 달라 저장된 판정을 쓸 수 없습니다. "
                     "파일을 옮기고 다시 실행하세요.")
    return data


def judge_call(query: str, chunks: list[dict]) -> list[bool | None]:
    from langchain_openai import ChatOpenAI

    from core.cost import TRACKER

    cfg = get_config()
    llm = ChatOpenAI(model=cfg.models.judge, temperature=cfg.models.temperature, max_retries=3, timeout=120,
                     callbacks=[TRACKER]).with_structured_output(Grades, method="json_schema", strict=True)
    res: Grades = llm.invoke(PROMPT.format(query=query, chunks=listing(chunks)))
    got = {j.idx: j.relevant for j in res.judgments}
    return [got.get(i) for i in range(len(chunks))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", help="API 키가 든 .env 경로 (값은 출력하지 않는다)")
    ap.add_argument("--budget", type=float, default=BUDGET_USD)
    ap.add_argument("--dry-run", action="store_true", help="검색 · 예상 비용만, API 호출 없음")
    ap.add_argument("--offline", action="store_true", help="저장된 판정만 사용 (없는 쌍이 있으면 실패)")
    args = ap.parse_args()
    if args.env:
        from dotenv import load_dotenv

        load_dotenv(args.env)

    import tiktoken

    from core.cost import TRACKER, model_price
    from rag.index import _corpus_hash, get_chunks

    cfg = get_config()
    ck, k = cfg.rag.candidate_k, cfg.rag.top_k
    logs = [(f, json.loads(path(f).read_text(encoding="utf-8"))) for f in RUN_LOGS if path(f).exists()]
    queries = collect_queries(logs)
    meta = {chunk_key(c.metadata): {"key": chunk_key(c.metadata), "doc_id": c.metadata["doc_id"],
                                    "chunk_id": c.metadata["chunk_id"], "page": c.metadata.get("page"),
                                    "lang": c.metadata.get("lang"), "publisher": c.metadata.get("publisher"),
                                    "year": c.metadata.get("year"), "text": c.page_content} for c in get_chunks()}
    print(f"질의 {len(queries)}개 (문서 {sum(q['kind'] == 'doc' for q in queries)} · "
          f"웹 {sum(q['kind'] == 'web' for q in queries)}), 조각 {len(meta)}개")

    specs = retrievers(ck)
    ranked = {name: {q["qid"]: [chunk_key(d.metadata) for d in r.invoke(q["query"])[:k]] for q in queries}
              for name, r in specs.items()}

    store = load_judgments(_corpus_hash())
    judged = {(j["query"], j["chunk"]): j["relevant"] for j in store["judgments"]}
    calls = plan_calls(queries, ranked, set(judged))
    qtext = {q["qid"]: q["query"] for q in queries}

    # 예상 비용 (호출 전): 실제 프롬프트를 그대로 만들어 토큰을 센다. 처음부터 다시 판정할 때(캐시 없음)와 이번 실행분을 함께 적는다
    enc = tiktoken.get_encoding("o200k_base")
    pi, po = model_price(cfg.models.judge)

    def estimate(plan: list[tuple[str, list[str]]]) -> tuple[dict, dict[str, float]]:
        by_qid: dict[str, float] = {}
        t_in = t_out = 0
        for qid, keys in plan:
            ti = len(enc.encode(PROMPT.format(query=qtext[qid], chunks=listing([meta[x] for x in keys])))) + EST_IN_OVERHEAD
            to = EST_OUT_BASE + EST_OUT_PER_ITEM * len(keys)
            t_in, t_out = t_in + ti, t_out + to
            by_qid[qid] = by_qid.get(qid, 0.0) + (ti * pi + to * po) / 1e6 * EST_MARGIN
        return ({"calls": len(plan), "pairs": sum(len(keys) for _, keys in plan), "input_tokens": t_in,
                 "output_tokens": t_out, "usd": round(sum(by_qid.values()), 4)}, by_qid)

    est_note = (f"tiktoken o200k_base 로 센 프롬프트 + 호출당 {EST_IN_OVERHEAD} 토큰(스키마 등), "
                f"출력 {EST_OUT_BASE}+{EST_OUT_PER_ITEM}×조각, ×{EST_MARGIN} 여유, 가격 {pi}/{po} USD/1M")
    est_fresh, _ = estimate(plan_calls(queries, ranked, set()))
    est, cost_by_qid = estimate(calls)
    spent0 = sum(c["usage"]["usd"] for c in store["calls"])
    print(f"예상 비용(처음부터): 호출 {est_fresh['calls']}회 · 판정 쌍 {est_fresh['pairs']}개 · 입력 "
          f"{est_fresh['input_tokens']:,} · 출력 {est_fresh['output_tokens']:,} 토큰 → ${est_fresh['usd']:.4f}")
    print(f"이번 실행 예상: 호출 {est['calls']}회 · ${est['usd']:.4f} (이미 쓴 ${spent0:.4f}, 예산 ${args.budget:.2f})")
    dropped: list[str] = []
    if spent0 + est["usd"] > args.budget:
        keep = set(fit_budget(cost_by_qid, [q["qid"] for q in queries], args.budget - spent0))
        dropped = [q["qid"] for q in queries if q["qid"] not in keep]
        queries = [q for q in queries if q["qid"] in keep]
        calls = [c for c in calls if c[0] in keep]
        print(f"예산 초과 → 질의 {len(dropped)}개를 뺌 ({', '.join(dropped)}), 남은 예상 "
              f"${sum(cost_by_qid.get(q, 0) for q in keep):.4f}")
    if args.dry_run:
        return
    if calls and args.offline:
        sys.exit(f"--offline: 저장된 판정이 없는 쌍 {sum(len(keys) for _, keys in calls)}개 (호출 {len(calls)}회)")

    # 판정 (판정 없는 쌍만). 호출마다 파일에 저장해 중간에 멈춰도 다시 부르지 않는다
    n0 = len(store["calls"])
    for i, (qid, keys) in enumerate(calls, 1):
        if spent0 + TRACKER.usd > args.budget:
            sys.exit(f"실제 비용 ${spent0 + TRACKER.usd:.4f} 가 예산을 넘어 멈춤 (저장된 판정은 다음 실행에서 재사용)")
        before = TRACKER.summary()
        flags = judge_call(qtext[qid], [meta[x] for x in keys])
        if any(f is None for f in flags):  # 번호가 빠진 응답은 한 번 더 묻는다
            flags = judge_call(qtext[qid], [meta[x] for x in keys])
        after = TRACKER.summary()
        cid = n0 + i
        store["calls"].append({"call": cid, "query": qtext[qid], "chunks": keys,
                               "usage": {m: after[m] - before[m] for m in ("api_calls", "input_tokens", "output_tokens")}
                               | {"usd": round(after["usd"] - before["usd"], 6)}})
        for x, f in zip(keys, flags):
            m = meta[x]
            store["judgments"].append({"query": qtext[qid], "chunk": x, "doc_id": m["doc_id"], "chunk_id": m["chunk_id"],
                                       "page": m["page"], "lang": m["lang"], "relevant": bool(f), "missing": f is None,
                                       "call": cid})
            judged[(qtext[qid], x)] = bool(f)
        path(JUDGMENTS).write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        if i % 10 == 0 or i == len(calls):
            print(f"  판정 {i}/{len(calls)} · 누적 ${TRACKER.usd:.4f}")
    if not store["calls"] and not path(JUDGMENTS).exists():
        path(JUDGMENTS).write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")

    # 지표
    qids = [q["qid"] for q in queries]
    rel_langs = {q["qid"]: {meta[x]["lang"] for r in ranked.values() for x in r[q["qid"]] if judged[(q["query"], x)]}
                 for q in queries}
    groups = {"all": qids,
              **{f"doc_lang:{g}": [q for q in qids if doc_lang(rel_langs[q]) == g] for g in DOC_LANG_LABEL},
              **{f"kind:{g}": [q["qid"] for q in queries if q["kind"] == g] for g in ("doc", "web")},
              **{f"query_lang:{g}": [q["qid"] for q in queries if _qlang(q["query"]) == g] for g in ("ko", "en")}}
    runtime = next(iter(specs))
    rows, per_q = [], {}
    for name, r in ranked.items():
        flags = {q["qid"]: [judged[(q["query"], x)] for x in r[q["qid"]]] for q in queries}
        langs = {q: [meta[x]["lang"] for x in r[q]] for q in qids}
        per_q[name] = flags
        rows.append({"name": name, "runtime": name == runtime,
                     **{g: summarize(flags, langs, ids, k) for g, ids in groups.items()}})
    best = pick(rows, runtime)
    base_p = [precision_at_k(per_q[runtime][q], k) for q in qids]
    vs = {r["name"]: paired(base_p, [precision_at_k(per_q[r["name"]][q], k) for q in qids])
          for r in rows if r["name"] != runtime}
    actual = TRACKER.summary()
    spent = round(sum(c["usage"]["usd"] for c in store["calls"]), 4)
    res = {n: {q["qid"]: [{"chunk": x, "lang": meta[x]["lang"], "relevant": judged[(q["query"], x)]} for x in r[q["qid"]]]
               for q in queries} for n, r in ranked.items()}
    diff = {n: diff_breakdown(res[runtime], res[n]) for n in ranked if n != runtime}
    check = runtime_agreement(logs, {q["query"]: per_q[runtime][q["qid"]] for q in queries}, k)
    out = {"rule": RULE, "pick": best["name"], "runtime": runtime, "k": k, "candidate_k": ck,
           "judge": {"model": cfg.models.judge, "temperature": cfg.models.temperature, "batch": BATCH,
                     "max_chars": MAX_CHARS, "prompt_sha": prompt_sha(), "prompt": PROMPT},
           "queries_total": len(queries) + len(dropped), "queries_used": len(queries), "dropped_for_budget": dropped,
           "cost": {"budget_usd": args.budget, "estimate_note": est_note, "estimate_fresh": est_fresh,
                    "estimate_this_run": est, "actual_all_judgment_calls": {
                        "calls": len(store["calls"]), "usd": spent,
                        **{m: sum(c["usage"][m] for c in store["calls"]) for m in ("input_tokens", "output_tokens")}},
                    "this_run": actual},
           "rows": rows, "paired_vs_runtime_P@4": vs, "diff_vs_runtime": diff, "judge_check_vs_runtime_grader": check,
           "queries": [{**q, "doc_lang": doc_lang(rel_langs[q["qid"]]), "results": {n: res[n][q["qid"]] for n in res}}
                       for q in queries]}
    path(OUT_JSON).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_md(out, groups)
    path(OUT_MD).write_text(md, encoding="utf-8")
    print(md)


def _qlang(q: str) -> str:
    from rag.hybrid_lang import query_lang

    return query_lang(q)


def render_md(out: dict, groups: dict) -> str:
    rows, k = out["rows"], out["k"]
    n_doc = groups.get("kind:doc", [])
    lines = ["# 실제 에이전트 질의로 잰 검색기 성능", "",
             f"질의 {out['queries_used']}개(문서 질의 {len(n_doc)} · 웹 질의 {len(groups.get('kind:web', []))}, "
             f"본 실행·보정 실행 rag_traces 에서 중복 제거), 검색기마다 앞 {k}개 조각을 "
             f"{out['judge']['model']}(temperature {out['judge']['temperature']})가 관련 O/X 로 판정 "
             f"(질의 하나 + 조각 최대 {out['judge']['batch']}개를 한 호출, 같은 질의·조각 쌍은 한 번만).", "",
             f"판정 규칙(측정 전에 고정): {out['rule']}", "",
             f"**선택: {out['pick']}**", "",
             "## 전체", "",
             "| 검색기 | Precision@4 | Hit@4 | MRR@4 (참고) | 앞 4개 중 영어 조각 비율 (참고) |", "|---|---|---|---|---|"]
    for r in rows:
        a = r["all"]
        lines.append(f"| {r['name']}{' **(선택)**' if r['name'] == out['pick'] else ''} | {a['P@4']:.3f} | "
                     f"{a['Hit@4']:.3f} | {a['MRR@4']:.3f} | {a['en_share']:.3f} |")
    for title, prefix, labels in (
            ("질의가 다루는 문서 언어별 (관련 판정을 받은 조각의 언어)", "doc_lang", DOC_LANG_LABEL),
            ("질의 종류별", "kind", {"doc": "문서 질의", "web": "웹 질의(문서 검색 적용)"}),
            ("질의 언어별", "query_lang", {"ko": "한국어 질의", "en": "영어 질의"})):
        cols = [g for g in labels if rows[0].get(f"{prefix}:{g}", {}).get("n")]
        lines += ["", f"## {title}", "", "Precision@4 · Hit@4", "",
                  "| 검색기 | " + " | ".join(f"{labels[g]} (n={rows[0][f'{prefix}:{g}']['n']})" for g in cols) + " |",
                  "|---|" + "---|" * len(cols)]
        for r in rows:
            lines.append(f"| {r['name']} | " + " | ".join(
                f"{r[f'{prefix}:{g}']['P@4']:.3f} · {r[f'{prefix}:{g}']['Hit@4']:.3f}" for g in cols) + " |")
    lines += ["", "## 런타임(A) 대비 질의별 Precision@4 차이 (참고, 판정 규칙에는 쓰지 않음)", "",
              "| 검색기 | 승 | 패 | 동 | 평균 차이 | 부트스트랩 95% 구간 |", "|---|---|---|---|---|---|"]
    for name, v in out["paired_vs_runtime_P@4"].items():
        lines.append(f"| {name} | {v['win']} | {v['loss']} | {v['tie']} | {v['mean_diff']:+.3f} | "
                     f"[{v['ci95'][0]:+.3f}, {v['ci95'][1]:+.3f}] |")
    lines += ["", "## 차이를 만든 조각 (참고): 한쪽 앞 4개에만 있는 조각의 언어 · 관련 판정 수", "",
              "| 비교 | 런타임(A)에만: 한국어 관련/비관련 · 영어 관련/비관련 | 상대에만: 한국어 관련/비관련 · 영어 관련/비관련 |",
              "|---|---|---|"]
    for name, d in out["diff_vs_runtime"].items():
        a, b = d["base"], d["other"]
        lines.append(f"| A vs {name} | {a['ko_rel']}/{a['ko_not']} · {a['en_rel']}/{a['en_not']} | "
                     f"{b['ko_rel']}/{b['ko_not']} · {b['en_rel']}/{b['en_not']} |")
    j = out["judge_check_vs_runtime_grader"]
    if j.get("queries"):
        lines += ["", "## Judge 점검 (참고): 본 실행의 런타임 Judge 와 일치도", "",
                  f"런타임 검색기 앞 4개 위치에서, 본 실행 run_log 의 첫 grade 단계(dropped 0, 하위 질문 기준 판정)와 "
                  f"이 평가 Judge(질의 기준)를 비교: 질의 {j['queries']}개 · {j['pairs']}쌍, 둘 다 관련 {j['both']} · "
                  f"둘 다 비관련 {j['neither']} · 이 평가만 관련 {j['ours_only']} · 런타임만 관련 {j['runtime_only']} → "
                  f"일치율 {j['agreement']:.3f}, Cohen's kappa {j['kappa']:.3f}. "
                  "(본 실행의 검색 순서가 지금 런타임 검색기와 같다고 가정)"]
    c = out["cost"]
    e, t, a = c["estimate_fresh"], c["estimate_this_run"], c["actual_all_judgment_calls"]
    lines += ["", "## 비용", "",
              f"- 호출 전 예상(처음부터 판정할 때): 호출 {e['calls']}회 · 판정 쌍 {e['pairs']}개 · 입력 "
              f"{e['input_tokens']:,} · 출력 {e['output_tokens']:,} 토큰 → ${e['usd']:.4f} (예산 ${c['budget_usd']:.2f}; "
              f"{c['estimate_note']})",
              f"- 실측(저장된 판정 호출 전체): 호출 {a['calls']}회 · 입력 {a['input_tokens']:,} · 출력 "
              f"{a['output_tokens']:,} 토큰 → ${a['usd']:.4f}",
              f"- 이번 실행: 예상 호출 {t['calls']}회 · ${t['usd']:.4f}, 실제 API 호출 {c['this_run']['api_calls']}회 · "
              f"${c['this_run']['usd']:.4f}"]
    if out["dropped_for_budget"]:
        lines.append(f"- 예산 때문에 뺀 질의: {', '.join(out['dropped_for_budget'])}")
    lines += ["", f"판정 기록: {JUDGMENTS} (질의 · 조각 id · 관련 여부 · 호출 번호). 다시 계산: "
                  "`python -m eval.eval_runtime_queries --offline`"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
