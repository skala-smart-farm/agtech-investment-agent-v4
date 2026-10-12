"""보고서 문장 충실성 검사(core/faithfulness.py)와 포털 전재본 원문 찾기(tools/origin.py).

2026-10-12 수업 피드백: 각주가 달렸다고 근거가 되지 않는다(반대되는 두 문장에 같은 레퍼런스), 2·3차 자료 대신 원 소스를 확인하라.
LLM·검색은 가짜로 바꿔 끼운다(유료 호출 없음)."""
from __future__ import annotations

import pytest

import agents.report as R
import tools.web_search as WS
from core import faithfulness as F
from tests.test_report import INVEST, _load, can_render, run  # noqa: F401  (run·can_render 는 픽스처)
from tools.origin import resolve_origin
from tools.sources import SourceRegistry


def test_best_windows_picks_the_part_that_matches_the_sentence():
    text = "가" * 3000 + " BIS Research 2023년 174억 달러 2034년 1,172억 달러 연평균 19.1% " + "나" * 3000
    w = F.best_windows("2034년 1,172억 달러로 연평균 19.1% 성장", text, n=1)
    assert len(w) == 1 and "1,172억" in w[0] and "19.1%" in w[0]
    assert F.best_windows("x", "짧은 원문") == ["짧은 원문"]


def test_check_counts_rate_and_judges_each_sentence_once(monkeypatch):
    calls = []

    def fake(sent, ids, text_of, source_of):
        calls.append(sent)
        return F.Support(supported="47.3%" not in sent, reason="근거에 47.3% 없음")
    monkeypatch.setattr(F, "judge_sentence", fake)
    units = [("2장 시장", "시장은 연평균 47.3% 성장한다", ["D1"]), ("SUMMARY 근거(시장)", "시장은 연평균 47.3% 성장한다", ["D1"]),
             ("3장 팀", "대표는 로봇을 전공했다", ["W1"])]
    out = F.check(units, lambda i: "", lambda i: "", 0.95)
    assert out["n"] == 3 and out["supported"] == 1 and out["rate"] == 0.333 and len(calls) == 2
    assert out["unsupported"][0].startswith("[2장 시장] 시장은 연평균 47.3% 성장한다 — ")


def test_citation_coverage_counts_numeric_sentences_without_citation():
    draft = R.Draft(**dict(INVEST, team="직원은 19명이다. 대표는 로봇을 전공했다 [Wc091e]. 최근 24개월 마일스톤은 확인 불가다."))
    cov = R._citation_coverage(draft)
    assert any("직원은 19명이다" in u for u in cov["uncited"])
    assert not any("24개월" in u for u in cov["uncited"])          # 평가 규칙 기간은 사실 숫자가 아니다
    assert cov["claims"] >= 1 and cov["rate"] < 1


@pytest.mark.faithfulness
def test_report_rewrites_once_when_faithfulness_below_target_and_accepts_only_if_better(run, set_cfg, monkeypatch):
    """목표(95%) 미달이면 근거가 뒷받침하지 않는 문장을 알려 주고 1회 다시 쓴다. 통과율이 올라야 새 초안을 받는다."""
    def fake(sent, ids, text_of, source_of):
        return F.Support(supported="47.3%" not in sent, reason="근거에 47.3% 없음")
    monkeypatch.setattr(F, "judge_sentence", fake)
    set_cfg("report.number_check", {"rewrite": False})
    bad = dict(INVEST, market=INVEST["market"] + " 국내 시장은 연평균 47.3% 성장한다 [D72ffc].")
    rep, md, llm = run(_load("invest"), [bad, INVEST])
    f = rep["checks"]["faithfulness"]
    assert len(llm.prompts) == 2 and "뒷받침하지 않는다" in llm.prompts[1] and "47.3%" in llm.prompts[1]
    assert f["rewrite"].startswith("채택") and f["rate"] == 1.0 and "47.3%" not in md
    # 다시 써도 그대로면(통과율이 오르지 않음) 기존 초안을 유지하고 미채택으로 기록
    rep2, _, llm2 = run(_load("invest"), [bad, bad])
    assert len(llm2.prompts) == 2 and rep2["checks"]["faithfulness"]["rewrite"].startswith("미채택")


def _portal_source(reg: SourceRegistry) -> dict:
    sid = reg.add_web({"url": "https://v.daum.net/v/FrX2b8I9zN", "title": "병충해 위험없는 로봇농부가 온다… 농업 혁신 꾀하는 메타파머스",
                       "published_date": "2023-11-12",
                       "content": "메타파머스는 ... 최효정 기자 Copyright ⓒ 조선비즈. All rights reserved."}, "tech", "q", "2026-09-30")
    return reg.get(sid)


def test_resolve_origin_finds_the_original_outlet_page(monkeypatch):
    s = _portal_source(SourceRegistry())
    found = [{"url": "https://www.daum.net/x", "title": "병충해 위험없는 로봇농부가 온다… 농업 혁신 꾀하는 메타파머스"},
             {"url": "https://biz.chosun.com/industry/2023/11/12/ABC/?daumarticle",
              "title": "병충해 위험없는 로봇농부가 온다… 농업 혁신 꾀하는 메타파머스 - 조선비즈"},
             {"url": "https://biz.chosun.com/other", "title": "다른 기사"}]
    monkeypatch.setattr(WS, "_raw_search", lambda *a, **k: found)
    assert resolve_origin(s) == {"url": "https://biz.chosun.com/industry/2023/11/12/ABC/", "site": "조선비즈"}


def test_resolve_origin_gives_up_offline_or_for_non_portal(monkeypatch):
    reg = SourceRegistry()
    s = _portal_source(reg)

    def offline(*a, **k):
        raise RuntimeError("--offline: 재현용 캐시에 없는 검색입니다")
    monkeypatch.setattr(WS, "_raw_search", offline)
    assert resolve_origin(s) is None
    w = reg.get(reg.add_web({"url": "https://zdnet.co.kr/view?no=1", "title": "t", "content": "c"}, "tech", "q", "2026-09-30"))
    assert resolve_origin(w) is None


def test_recite_keeps_sentence_and_fixes_citation(monkeypatch):
    """인용이 엉뚱한 기사에 달린 문장(사실은 다른 근거에 있음)은 문장을 그대로 두고 인용만 바꾼다."""
    texts = {"Wold": "메타파머스는 프리A 투자를 계획하고 있다", "Wnew": "메타파머스가 30억원 규모 프리A 투자를 유치했다",
             "Wx": "다른 회사 이야기"}

    def fake(sent, ids, text_of, source_of):
        return F.Support(supported="Wnew" in ids, reason="")
    monkeypatch.setattr(F, "judge_sentence", fake)
    assert F.recite("메타파머스는 30억원 프리A 투자를 유치했다", list(texts), texts.get, lambda i: "", k=1) == ["Wnew"]
    assert F.recite("메타파머스는 CES 혁신상을 받았다", ["Wx"], texts.get, lambda i: "", k=1) is None


def test_report_recite_replaces_only_the_citation(monkeypatch):
    reg = SourceRegistry()
    old = reg.add_web({"url": "https://a.kr/1", "title": "계획", "content": "메타파머스는 프리A 투자를 계획하고 있다"},
                      "tech", "q", "2026-09-30")
    new = reg.add_web({"url": "https://b.kr/2", "title": "유치", "content": "메타파머스가 30억원 규모 프리A 투자를 유치했다"},
                      "tech", "q", "2026-09-30")
    draft = R.Draft(**dict(INVEST, team=f"대표는 로봇을 전공했다 [{old}]. 메타파머스는 30억원 프리A 투자를 유치했다 [{old}]."))
    monkeypatch.setattr(F, "judge_sentence", lambda s, ids, t, so: F.Support(supported=("유치" not in s) or new in ids, reason=""))
    faith = R._faithfulness(draft, reg, 0.95)
    fixed, changed = R._recite(draft, faith, reg, {"evidence": f"[{old}] [{new}]"})
    assert changed and f"메타파머스는 30억원 프리A 투자를 유치했다 [{new}" in fixed.team
    assert f"대표는 로봇을 전공했다 [{old}]." in fixed.team and R._faithfulness(fixed, reg, 0.95)["rate"] == 1.0


def test_hold_reason_unknown_list_is_checked_against_scorecard_by_code():
    """'2장 보류 사유(회사)'의 미확인 항목 목록은 근거 기사가 아니라 그 회사 평가표와 코드로 대조한다 (LLM 호출 없음)."""
    evals = [{"name": "A", "rows": [{"qid": "F2", "answer": "UNKNOWN"}, {"qid": "F3", "answer": "UNKNOWN"},
                                    {"qid": "C1", "answer": "NO"}, {"qid": "R1", "answer": "YES"}]}]
    decide = R._scorecard_claim(evals)
    ok = decide("2장 보류 사유(A)", "기술 책임자 이력, 최근 마일스톤, 특허 확인 안 됨")
    bad = decide("2장 보류 사유(A)", "기술 책임자 이력, 최근 마일스톤, 매출 확인 안 됨")
    assert ok.supported and not bad.supported and "매출" in bad.reason
    assert decide("3장 팀", "기술 책임자 이력, 최근 마일스톤, 특허") is None          # 보류 사유가 아니면 LLM 판정
    assert decide("2장 보류 사유(A)", "매출이 없다") is None                       # 목록(3개 이상)이 아니면 LLM 판정


def test_enforce_unknown_lists_removes_items_the_scorecard_says_yes():
    evals = [{"name": "A", "rows": [{"qid": "D4", "answer": "YES"}, {"qid": "F2", "answer": "YES"}, {"qid": "R1", "answer": "UNKNOWN"},
                                    {"qid": "R4", "answer": "UNKNOWN"}, {"qid": "F3", "answer": "UNKNOWN"}]}]
    note = R.CandidateNote(name="A", business="농업 로봇",
                           why_not="기술 책임자 이력, 최근 마일스톤, 민간 매출, 매출, 자본집약도 등 공개 근거 없음 [W1a2b3]")
    draft = R.HoldDraft.model_construct(candidates=[note])
    fixed, log = R._enforce_unknown_lists(draft, evals)
    assert fixed.candidates[0].why_not == "최근 마일스톤, 민간 매출, 매출 등 공개 근거 없음 [W1a2b3]"
    assert log == ["A: 평가표 YES 문항을 미확인 목록에서 뺌 — 기술 책임자 이력, 자본집약도"]
    assert R._enforce_unknown_lists(fixed, evals)[1] == []
