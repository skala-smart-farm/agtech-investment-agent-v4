"""실제 질의 검색 평가(eval/eval_runtime_queries.py)의 지표·수집·묶음·판정 규칙 (LLM·검색 없음)."""
from __future__ import annotations

import pytest

from eval.eval_runtime_queries import (collect_queries, diff_breakdown, doc_lang, fit_budget, hit_at_k, judge_order,
                                       paired, pick, plan_calls, precision_at_k, rr_at_k, runtime_agreement, summarize)


def test_precision_hit_rr():
    flags = [False, True, False, True, True]          # 5번째는 앞 4개 밖
    assert precision_at_k(flags, 4) == 0.5
    assert hit_at_k(flags, 4) == 1.0 and hit_at_k([False] * 4 + [True], 4) == 0.0
    assert rr_at_k(flags, 4) == 0.5 and rr_at_k([False] * 4, 4) == 0.0
    assert precision_at_k([True, True], 4) == 0.5    # 4개보다 적게 나와도 분모는 4


def test_summarize_means_and_english_share():
    flags = {"q1": [True, True, False, False], "q2": [False, False, False, False]}
    langs = {"q1": ["en", "en", "ko", "ko"], "q2": ["en", "ko", "ko", "ko"]}
    s = summarize(flags, langs, ["q1", "q2"], 4)
    assert s == {"n": 2, "P@4": 0.25, "Hit@4": 0.5, "MRR@4": 0.5, "en_share": 0.375}
    assert summarize(flags, langs, [], 4) == {"n": 0}


def test_doc_lang_groups():
    assert doc_lang(set()) == "none" and doc_lang({None}) == "none"
    assert doc_lang({"ko"}) == "ko" and doc_lang({"en"}) == "en" and doc_lang({"ko", "en"}) == "mixed"


def _log(*traces):
    return {"rag_traces": list(traces)}


def test_collect_queries_dedup_and_kinds():
    t1 = {"agent": "market", "question": "시장 규모는?", "trace": [
        {"step": "agent", "route": "both", "query": " 스마트팜 시장 규모 ", "web_query": "스마트팜 시장 규모 2026"},
        {"step": "grade", "query": " 스마트팜 시장 규모 "},
        {"step": "rewrite", "query": "스마트팜 시장 규모 CAGR"},       # 재작성 단계 자체는 검색 기록이 아님
        {"step": "grade", "query": "스마트팜 시장 규모 CAGR"},
        {"step": "generate", "context": 4},
        {"step": "web", "query": "스마트팜 시장 규모 2026"}]}
    t2 = {"agent": "discovery", "question": "사례", "trace": [{"query": "애그테크 사례", "retrieved": 8}]}  # v1 경로
    decomp = {"agent": "market", "step": "decompose", "question": "시장성", "trace": [], "sub_questions": ["a"]}
    qs = collect_queries([("a.json", _log(t1, decomp, t2)), ("b.json", _log(t1))])
    # 첫 등장 순서: agent 단계의 web_query → grade 질의(앞뒤 공백 제거) → 재작성 뒤 grade 질의 → v1 경로 질의
    assert [q["query"] for q in qs] == ["스마트팜 시장 규모 2026", "스마트팜 시장 규모", "스마트팜 시장 규모 CAGR", "애그테크 사례"]
    assert [q["kind"] for q in qs] == ["web", "doc", "doc", "doc"]   # web 단계에서 다시 나와도 한 번만
    assert [q["qid"] for q in qs] == ["q01", "q02", "q03", "q04"]
    assert qs[1]["sources"] == ["a.json", "b.json"] and qs[1]["sub_question"] == "시장 규모는?"


def test_judge_order_union_best_rank_first():
    ranked = {"A": ["x", "y", "z", "w"], "B": ["y", "x", "v", "z"], "C": ["v", "u", "x", "y"]}
    assert judge_order(ranked) == ["x", "y", "v", "u", "z", "w"]   # 1위 x(A)·y(B)·v(C) → 2위 u(C) → 3위 z → 4위 w


def test_plan_calls_skips_judged_pairs_and_batches_by_four():
    qs = [{"qid": "q1", "query": "Q1"}, {"qid": "q2", "query": "Q2"}]
    ranked = {"A": {"q1": ["a", "b", "c", "d"], "q2": ["a", "b", "c", "d"]},
              "B": {"q1": ["e", "a", "b", "c"], "q2": ["a", "b", "c", "d"]}}
    calls = plan_calls(qs, ranked, judged={("Q1", "b"), ("Q2", "a")})
    # q1: 합집합 a,e,b,c,d 중 b 는 판정됨 → [a,e,c,d] 한 번. q2: 같은 조각이어도 질의가 다르면 따로 판정
    assert calls == [("q1", ["a", "e", "c", "d"]), ("q2", ["b", "c", "d"])]
    assert plan_calls(qs, ranked, judged={(q, x) for q in ("Q1", "Q2") for x in "abcde"}) == []


def _row(name, p, h):
    return {"name": name, "all": {"P@4": p, "Hit@4": h}}


def test_pick_rule_precision_then_hit_then_runtime():
    assert pick([_row("A", 0.5, 0.9), _row("B", 0.6, 0.8)], "A")["name"] == "B"            # 1) P@4
    assert pick([_row("A", 0.6, 0.8), _row("B", 0.6, 0.9)], "A")["name"] == "B"            # 2) 같으면 Hit@4
    assert pick([_row("B", 0.6, 0.9), _row("A", 0.6, 0.9)], "A")["name"] == "A"            # 3) 완전 동률 → 런타임
    assert pick([_row("A", 0.5, 0.9), _row("B", 0.6, 0.9), _row("C", 0.6, 0.9)], "A")["name"] == "B"  # 그다음 앞쪽


def test_paired_counts_and_interval():
    v = paired([0.5, 0.5, 0.5, 0.25], [0.75, 0.5, 0.25, 0.5])
    assert (v["win"], v["loss"], v["tie"]) == (2, 1, 1)
    assert v["mean_diff"] == pytest.approx(0.0625, abs=1e-3)
    assert v["ci95"][0] <= v["mean_diff"] <= v["ci95"][1]
    assert paired([0.5] * 3, [0.5] * 3)["ci95"] == [0.0, 0.0]


def test_fit_budget_keeps_prefix_within_budget():
    assert fit_budget({"q1": 0.05, "q2": 0.1, "q3": 0.1}, ["q1", "q2", "q3"], 0.2) == ["q1", "q2"]
    assert fit_budget({"q1": 0.05}, ["q1", "q2"], 0.2) == ["q1", "q2"]   # 판정할 것이 없는 질의는 비용 0


def _r(chunk, lang, rel):
    return {"chunk": chunk, "lang": lang, "relevant": rel}


def test_diff_breakdown_counts_only_one_side():
    base = {"q1": [_r("a", "ko", True), _r("b", "ko", False), _r("c", "en", True)]}
    other = {"q1": [_r("c", "en", True), _r("d", "en", False), _r("e", "en", True)]}
    d = diff_breakdown(base, other)
    assert d["base"] == {"ko_rel": 1, "ko_not": 1, "en_rel": 0, "en_not": 0}     # 공통 c 는 세지 않음
    assert d["other"] == {"ko_rel": 0, "ko_not": 0, "en_rel": 1, "en_not": 1}


def test_runtime_agreement_uses_first_grade_without_drops():
    log = {"rag_traces": [
        {"trace": [{"step": "agent", "query": "Q1"},
                   {"step": "grade", "query": "Q1", "dropped": 0, "kept_ranks": [1, 3, 6]},   # 6위는 앞 4개 밖
                   {"step": "grade", "query": "Q1b", "dropped": 0, "kept_ranks": [1]}]},      # 두 번째 grade 는 안 씀
        {"trace": [{"step": "grade", "query": "Q2", "dropped": 2, "kept_ranks": [1, 2, 3, 4]}]},  # dropped>0 제외
        {"trace": [{"step": "grade", "query": "Q1", "dropped": 0, "kept_ranks": [2]}]}]}         # 같은 질의는 한 번만
    flags = {"Q1": [True, True, False, False], "Q1b": [True] * 4, "Q2": [True] * 4}
    a = runtime_agreement([("x", log)], flags, 4)
    assert (a["queries"], a["pairs"]) == (1, 4)
    assert (a["both"], a["ours_only"], a["runtime_only"], a["neither"]) == (1, 1, 1, 1)
    assert a["agreement"] == 0.5 and a["kappa"] == 0.0
    assert runtime_agreement([("x", {"rag_traces": []})], flags, 4) == {"queries": 0, "both": 0, "ours_only": 0,
                                                                         "runtime_only": 0, "neither": 0}
