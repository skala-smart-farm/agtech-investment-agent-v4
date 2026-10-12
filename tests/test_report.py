"""📝 보고서 에이전트 테스트 (P5, 키·네트워크 없이).

- State: tests/fixtures/state_invest.json(A안: 투자 추천 1곳) · state_hold.json(B안: 10곳 모두 보류, 시나리오 기준 1.30)
  계약 C7·C8 형식이며 v1 실행의 실제 후보·근거로 만들었다(픽스처의 _note 참고).
- LLM(structured)은 미리 쓴 초안을 순서대로 돌려주는 가짜로 바꾼다. 첫 초안을 일부러 규칙 위반으로 주면 재작성 루프를 확인한다.
- PDF: Playwright·Chromium 이 있으면 실제로 렌더링해 쪽수·SUMMARY 높이를 잰다. 없으면 PDF 단계만 가짜로 바꾸고 쪽수 검사는 건너뛴다.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents.report as R
import tools.web_search as WS
from tools.sources import (ACCESS_DATE_NOTE, REFERENCE_FORMATS, SourceRegistry, format_reference, reference_group,
                           uses_access_date)

FIX = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIX / f"state_{name}.json").read_text(encoding="utf-8"))


# ── 가짜 초안 (근거 id 는 픽스처 근거 저장소에 있는 것만)
NOTES = [R.CompetitorNote(name="긴트", vs_target="긴트는 트랙터 부착형 자율주행 키트이고, 대상은 과채류 수확·수분 로봇이라 적용 작업이 다르다 [W2a166]"),
         R.CompetitorNote(name="에이지로보틱스", vs_target="에이지로보틱스는 자율주행 농작업 로봇, 대상은 수확·선별에 맞춘 그리퍼형 로봇이다 [W2a166]"),
         R.CompetitorNote(name="Blue White Robotics", vs_target="기존 트랙터를 개조하는 키트 방식으로, 대상의 전용 로봇 방식과 접근이 다르다 [Wb4094]"),
         R.CompetitorNote(name="Sabanto", vs_target="트랙터 레트로핏 자율주행 기술로, 대상과 적용 작업(노지 경운 대 온실 수확)이 다르다 [Wb591a]")]
RISKS = [R.Risk(type="시장", content="초기 도입 비용과 운영비 부담이 농가 도입을 늦출 수 있다", evidence_ids=["D620cb"],
                due_diligence="농가당 도입 비용·구독 요금과 유료 전환율"),
         R.Risk(type="기술", content="옴니파머는 2026년 10월 상용화 예정으로, 여러 작기·지역의 성능 검증 자료가 없다", evidence_ids=["Wbf33c"],
                due_diligence="작물별 수확 성공률의 제3자 측정 자료"),
         R.Risk(type="규제", content="스마트농업 육성 기본계획에 따라 농업용 로봇 관련 기준이 정비되는 중이다", evidence_ids=["Db474a"],
                due_diligence="농업기계 검정 대상 여부와 취득 일정"),
         R.Risk(type="경쟁", content="자율주행 농기계·농업 로봇 스타트업이 국내외에 다수 있다", evidence_ids=["Wb4094"],
                due_diligence="직접 경쟁 제품(수확 로봇) 대비 가격·성능 비교 자료")]
BODY = dict(
    problem="농업 현장의 인력난과 외국인 노동자 관리 부담으로 수확 등 반복 작업을 자동화할 필요가 있다 [Wc3a78].",
    product="AI 비전 인식과 교체형 그리퍼로 수확·수분·선별을 하는 범용 농작업 로봇 '옴니파머' [W2a166, Wc091e].",
    revenue_model="회사 측 설명에 따르면 구독형 요금제와 수확량 1kg당 요금 부과 방식을 계획하고 있다 [Wc3a78].",
    market="글로벌 애그테크 산업은 2020년 91억 달러에서 2025년 226억 달러로 연평균 20% 성장한다는 예측이 있었다(Statista 추정, 2022년 발표 전망) [Dd31ce]. "
           "국내 스마트농업 장비·서비스 시장은 2021년 기준 3천억 원 규모다(농림축산식품부 추정) [D72ffc].",
    team="이규화 대표는 창업 전 서울대 기계공학부 박사 과정에서 스마트팩토리 구축 경험을 쌓았고 [Wc091e], 윤원재 CTO 등 공동창업자는 AI 로봇·엔드이펙터를 전공했다 [Wc091e].",
    industry_baseline="업계 문헌은 농업 로봇이 작물·환경 변동성 때문에 해결할 과제가 많다고 보며 [D019bc], 메타파머스는 2026년 10월 상용화를 앞둔 실증 단계다 [Wbf33c].",
    competition="표의 경쟁사는 자율주행 농기계·트랙터 키트 중심이라 대상(온실 과채류 수확·수분 로봇)과 제품 범위가 다르다 [Wb4094, Wb591a].",
    competitor_notes=NOTES, risks=RISKS, data_limits=["매출·고객 수·기업가치가 비공개라 실적과 투자 조건을 평가할 수 없다"])
INVEST = dict(BODY,
              ev_tech="수확·수분·선별 범용 로봇 옴니파머, 프리A 30억 원 유치 [W2a166, Wef018]",
              ev_market="국내 스마트농업 장비·서비스 시장 3천억 원(2021년) [D72ffc]",
              ev_team="대표·CTO 모두 창업 전 로봇·자동화 전공·경력 [Wc091e]",
              risk_line="상용화 전(2026년 10월 예정)이라 유료 고객·제3자 실증 근거가 없다 [Wbf33c]",
              lead_idea="범용 농작업 로봇으로 수확 인력 문제를 겨냥하며, 상용화는 2026년 10월 예정이다.",
              lead_market="국내 시장 규모는 작지만 스마트농업 정책 지원이 확대되고 있다.",
              lead_team="대표·CTO 모두 창업 전부터 로봇·자동화 분야 경력을 쌓았다.")
CANDS = {"메타파머스": "제3자 실증은 PoC 단계라는 보도만 있고 농협 등과의 협력도 기술 검증 단계다 [W2a166]. 매출·유료 고객 수는 공개 근거가 없다.",
         "Nature Robots": "창업팀·기술 책임자 이력과 매출·고객 수가 공개 근거로 확인되지 않았다.",
         "리비타": "최근 마일스톤은 2026년 4월 시드 투자 1건만 확인됐다 [W903d5]. 창업팀 이력과 매출은 공개 근거가 없다.",
         "새팜": "대표·기술 책임자의 이력이 공개 근거에서 확인되지 않았고 매출과 유료 고객 규모도 미확인이다.",
         "Upside Robotics": "창업팀 이력과 상용 운영 규모가 공개 근거로 확인되지 않았다.",
         "퓨처커넥트": "최근 마일스톤·제3자 교차 확인·경제적 효과가 공개 근거로 확인되지 않았다.",
         "트랙팜": "최근 라운드가 24개월보다 오래됐고 창업팀 이력은 공개 근거가 없다.",
         "Bonsai Robotics": "창업팀 이력과 매출·고객 수가 공개 근거로 확인되지 않았다.",
         "에임비랩": "최근 투자 라운드가 24개월을 넘었고 창업자 이력은 공개 근거가 없다.",
         "바르카": "상용 운영이 확인되지 않았고 기술 책임자 이력도 공개 근거가 없다."}
HOLD = dict(BODY,
            common_cause="대부분 Seed~Pre-A 단계라 창업팀 이력·매출·유료 고객 규모의 공개 근거가 적다 [Wef018, W903d5]",
            common_detail="평가한 후보는 대부분 투자 유치 외 실적 공개가 적은 초기 기업이다 [Wef018, W903d5]. "
                          "국내 스마트농업 장비·서비스 시장은 2021년 기준 3천억 원 규모로 [D72ffc] 정책 지원이 이어지고 있다 [Db474a].",
            candidates=[R.CandidateNote(name=n, business="", why_not=w) for n, w in CANDS.items()])


class _FakeLLM:
    """structured(schema).invoke(prompt) 가짜: 준비한 초안을 차례로 돌려준다(마지막 것은 계속 반복)."""

    def __init__(self, drafts: list[dict]):
        self.drafts, self.prompts = list(drafts), []

    def __call__(self, schema, role: str = "generator", kind: str = "default"):
        fake = self

        class _Runner:
            def invoke(self, prompt):
                fake.prompts.append(prompt)
                data = fake.drafts.pop(0) if len(fake.drafts) > 1 else fake.drafts[0]
                return schema(**{k: v for k, v in data.items() if k in schema.model_fields})
        return _Runner()


@pytest.fixture(scope="session")
def can_render() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # Playwright·Chromium 이 없는 환경
        return False


def _write_reference(path: Path) -> None:
    """보정 실행이 쓰는 기준 집단 파일(계약 C10 형식)을 B안 픽스처의 평가 10곳으로 만든다 (단계·지역 설명용)."""
    evals = _load("hold")["evaluations"]
    members = [{"name": e["name"], "region": e["region"], "stage": e["stage"], "segment_id": e["segment_id"],
                "signals": {r["qid"]: r["x"] for r in e["scorecard"]["rows"]}} for e in evals]
    path.write_text(json.dumps({"version": 1, "run_date": "2026-09-30", "created_by": "app.py --calibrate",
                                "n": len(members), "members": members, "mean": {}}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def run(monkeypatch, tmp_path, set_cfg, can_render):
    """report_node 를 가짜 LLM 으로 돌린다. 결과 파일은 임시 폴더에 쓴다(저장소 outputs/ 를 건드리지 않음)."""
    out = tmp_path / "out"
    set_cfg("report.output_dir", str(out))
    ref = tmp_path / "reference_class.json"
    _write_reference(ref)
    set_cfg("decision.reference_file", str(ref))
    monkeypatch.setattr(WS, "FAILED_QUERIES", [])
    if not can_render:
        def fake_pdf(html, pdf_path):
            pdf_path.with_suffix(".html").write_text(html, encoding="utf-8")
            pdf_path.write_bytes(b"%PDF-1.4 fake")
            return {"pages": 1, "summary_ratio": 0.1, "html": str(pdf_path.with_suffix(".html"))}
        monkeypatch.setattr(R, "html_to_pdf", fake_pdf)

    def _run(state: dict, drafts: list[dict]):
        llm = _FakeLLM(drafts)
        monkeypatch.setattr(R, "bounded", llm)
        rep = R.report_node(copy.deepcopy(state))["report"]
        md = Path(rep["md"]).read_text(encoding="utf-8") if rep.get("md") else ""
        return rep, md, llm
    return _run


def _heads(md: str) -> list[str]:
    return re.findall(r"^## (.+)$", md, re.M)


def _summary(md: str) -> list[str]:
    return [ln for ln in md.split("## SUMMARY", 1)[1].split("\n## ", 1)[0].splitlines() if ln.startswith("- ")]


def _refs(md: str) -> list[str]:
    return [ln for ln in md.split("## REFERENCE", 1)[1].splitlines() if re.match(r"\d+\. ", ln)]


# ── A안: 투자 추천

def test_invest_report_structure(run, can_render):
    state = _load("invest")
    rep, md, llm = run(state, [INVEST])
    ck = rep["checks"]
    assert rep["mode"] == "invest" and len(llm.prompts) == 1, ck["consistency_violations"]
    assert _heads(md) == list(R.A_CHAPTERS)  # 장 제목 순서 = A안 고정 목록 (본문 5개 장 = 과제 주요 내용 5항목)
    lines = _summary(md)
    assert [ln.split(":", 1)[0] for ln in lines] == ["- 상황", "- 결론", "- 근거", "- 리스크", "- 요청"]
    sc = state["evaluations"][0]["scorecard"]
    concl = lines[1]
    assert concl.startswith("- 결론: 메타파머스 투자 추천(실사 조건부)")
    # 픽스처는 설계 기준(M > 1.00 ∧ 창업자 기준 ≥ 1.00)의 투자 결과다
    assert sc["threshold"] == 1.00 and sc["multiplier"] > 1.00 and sc["founder_c"] >= 1.00 and sc["hold_type"] is None
    assert f"동종 평균 대비 {sc['multiplier'] * 100:.1f}(동종 평균 100 초과, 창업자 기준 {sc['founder_c'] * 100:.1f})" in concl
    assert f"동종 {sc['peer_n']}곳 중 {sc['target_rank']}위" in concl
    assert lines[4] == f"- 요청: 실사 확인 항목 {len(sc['dd_items'])}개를 조건으로 투자심의 상정을 진행할까요?"
    assert "(→1장)" in lines[2] and "(→2장)" in lines[2] and "(→3장)" in lines[2] and "(→4장)" in lines[3]
    for key in ("chapter_refs_ok", "required_items_ok", "refs_match_citations", "reference_format_ok",
                "conclusion_matches_scorecard", "assumptions_labeled"):
        assert ck[key] is True, (key, ck)
    assert ck["banned_hits"] == [] and ck["consistency_violations"] == [] and ck["summary_rule_violations"] == []
    assert ck["first_section"] == "SUMMARY" and ck["last_section"] == "REFERENCE"
    # 4장: 기준표·Bessemer 한 줄·순위 상위 3·민감도 한 줄·ROI(가정 표기)·실사 항목
    ch4 = md.split("## 4. 투자 판단과 사업 리스크", 1)[1].split("\n## ", 1)[0]
    assert ch4.count("Bessemer 10문:") == 1 and ch4.count("기준 민감도(what-if, 결정에 쓰지 않음):") == 1
    assert ch4.lstrip().startswith(f"**동종 평균 대비 {sc['multiplier'] * 100:.1f}(동종 평균 100 초과), "
                                   f"창업자 기준 {sc['founder_c'] * 100:.1f}(동종 평균 이상), Deal-killer 없음")
    assert "투자 추천 ⇔ 동종 평균 대비 배수 > 1.00(표기 100) ∧ 창업자 기준 ≥ 동종 평균 ∧ Deal-killer 없음" in ch4
    assert "기준 1.00: Payne Scorecard 원 방법의 기준점 '투자를 받은 동종 평균 기업 = 100%'" in ch4
    # 옛 규칙(가정한 기준 1.10·창업자 YES 개수)의 문구가 남아 있지 않다
    assert not [t for t in ("1.1205", "Eqvista", "설계 기준", "기준 110", "창업자 근거", "창업자 문항 YES") if t in md]
    rank = next(ln for ln in ch4.splitlines() if "동종 순위(상위 3)" in ln)
    assert rank.count("위 ") == 3 + 1 and "대상 1위" in rank  # '상위 3' + 상위 3곳, 끝에 '대상 n위'
    assert re.search(r"1위 \S+ \d+\.\d\((투자|보류: [^)]+)\)", rank) and "투자 기준 통과" in rank   # 순위마다 판정, 소수 한 자리
    roi = [ln for ln in ch4.splitlines() if "post-money" in ln or "필요 Exit" in ln]
    assert roi and all("가정" in ln for ln in roi)
    assert "Pre-A 단계 중앙값 $1M의 2.1배" in ch4 and "AgFunder(2026)" in md.split("## REFERENCE", 1)[1]
    assert "exit_share" not in md and "회수 여력" not in md  # 시장 대비 Exit 경고는 설계에서 뺐다
    dd = [ln for ln in ch4.split("실사 확인 항목", 1)[1].splitlines() if ln.startswith("- ")]
    assert len(dd) == len(sc["dd_items"])
    assert "창업 시점 t0" in md and "2022-09-02" in md  # 3장 t0 표
    assert "Pre-A·Series A 혼합, 국내 7·해외 3의 근사 기준" in ch4 or "혼합, 국내" in ch4  # 기준 집단은 근사임을 밝힌다
    assert rep["submit_pdf"] and Path(rep["submit_pdf"]).exists()
    if can_render:
        assert rep["pages"] <= 5 and rep["summary_ratio"] <= 0.5, (rep["pages"], rep["summary_ratio"])


def test_invest_scenario_skips_submission_file(run, set_cfg):
    set_cfg("report.scenario", True)
    rep, _, _ = run(_load("invest"), [INVEST])
    assert rep["submit_pdf"] is None and Path(rep["pdf"]).exists()
    assert not list(Path(rep["pdf"]).parent.glob("RAG-Output_*"))


def test_summary_banned_phrase_is_reported(run):
    """SUMMARY 에 '이 보고서는' 같은 개요 문장이 끝까지 남으면 checks 위반으로 잡힌다."""
    bad = dict(INVEST, ev_tech="이 보고서는 메타파머스의 기술을 평가한다 [W2a166]")
    rep, md, llm = run(_load("invest"), [bad])
    ck = rep["checks"]
    assert len(llm.prompts) == 3  # 첫 초안 + 재작성 2회 (report.max_rewrites)
    assert "금지 표현 '이 보고서'" in llm.prompts[1]
    assert "이 보고서" in ck["banned_hits"] and any("이 보고서" in p for p in ck["summary_rule_violations"])
    assert ck["rewrites"] == 2


def test_superiority_without_c2_is_detected(run):
    """C2(경쟁사 대비 차별점)가 YES 가 아닌데 가짜 LLM 이 '앞선다'를 쓰면 검출하고, 고친 초안으로 다시 쓴다."""
    state = _load("invest")
    assert {r["qid"]: r["answer"] for r in state["evaluations"][0]["scorecard"]["rows"]}["C2"] != "YES"
    bad = dict(INVEST, competitor_notes=[R.CompetitorNote(name="긴트", vs_target="메타파머스는 범용 로봇으로 다양한 작업에 앞선다 [W2a166]")] + NOTES[1:])
    rep, md, llm = run(state, [bad, INVEST])
    assert len(llm.prompts) == 2 and "'앞선다'" in llm.prompts[1]
    assert rep["checks"]["consistency_violations"] == []
    assert "앞선다" not in md
    # 끝까지 고치지 않으면 표에는 회사 측 주장으로 표시되고 위반이 기록된다
    rep2, md2, _ = run(state, [bad])
    assert any("'앞선다'" in p for p in rep2["checks"]["consistency_violations"])
    assert "(회사 측 주장, 제3자 비교 근거 없음) 메타파머스는 범용 로봇으로 다양한 작업에 앞선다" in md2


def test_reference_format_and_citations(run, monkeypatch):
    monkeypatch.setattr(R, "resolve_origin", lambda s: None)   # 원 매체 기사를 못 찾은 경우 (찾은 경우는 아래 테스트)
    rep, md, _ = run(_load("invest"), [INVEST])
    refs = _refs(md)
    assert refs
    groups = md.split("## REFERENCE", 1)[1]
    assert groups.index("### 기관 보고서") < groups.index("### 학술 논문") < groups.index("### 웹페이지")
    for ln in refs:  # 3유형 형식, 줄 끝 괄호 주석 없음
        text = ln.split(". ", 1)[1]
        assert any(p.match(text) for p in REFERENCE_FORMATS.values()), text
        assert not re.search(r"\s\([^()]*\)$", text) and "조회일 표기" not in text, text
    nums = [int(ln.split(".")[0]) for ln in refs]
    assert nums == list(range(1, len(nums) + 1))  # 유형이 바뀌어도 번호가 이어진다
    body = md.split("## REFERENCE")[0]
    cited = {int(n) for m in re.findall(r"\[([\d, ]+)\]", body) for n in m.split(",")}
    assert cited == set(nums)  # 본문 인용 = REFERENCE (실제 활용 자료만)
    assert "stibee" not in md  # 뉴스레터는 인용하지 않는다
    assert sum("피지컬 AI'로 농업 혁명" in ln for ln in refs) == 1  # 포털 전재본과 원문은 한 줄
    daum = [ln for ln in refs if "v.daum.net/v/FrX2b8I9zN" in ln]
    assert daum and "다음뉴스" not in daum[0]  # 포털 전재본은 원 매체로 표기
    # 조회일 사실은 REFERENCE 가 아니라 한계점에 한 줄
    lim = md.split("## 5. 한계점", 1)[1].split("## REFERENCE")[0]
    assert sum(ACCESS_DATE_NOTE in ln for ln in lim.splitlines()) == 1
    assert ACCESS_DATE_NOTE not in groups
    assert rep["cited_ids"] and all(re.fullmatch(r"[WD][0-9a-f]{5}", i) for i in rep["cited_ids"])
    assert rep["checks"]["portal_refs"]["unresolved"] >= 1 and "포털 전재본" in lim and "원 매체 기사 주소를 찾지 못해" in lim


def test_portal_reference_uses_original_outlet_url_when_found(run, monkeypatch):
    """포털 전재본은 원 매체 기사를 찾으면 그 주소로 인용한다 (2·3차 자료 대신 원 소스 — 2026-10-12 수업 피드백)."""
    monkeypatch.setattr(R, "resolve_origin", lambda s: {"url": "https://biz.chosun.com/x/2023/11/12/ABC/", "site": "조선비즈"})
    rep, md, _ = run(_load("invest"), [INVEST])
    refs = _refs(md)
    assert not any("v.daum.net" in ln for ln in refs) and any("biz.chosun.com/x/2023/11/12/ABC/" in ln for ln in refs)
    assert rep["checks"]["portal_refs"]["unresolved"] == 0 and "원 매체 기사 주소를 찾지 못해" not in md


# ── B안: 모두 보류

def test_hold_report_structure(run, can_render):
    state = _load("hold")
    rep, md, llm = run(state, [HOLD])
    ck = rep["checks"]
    assert rep["mode"] == "hold" and len(llm.prompts) == 1, ck["consistency_violations"]
    heads = _heads(md)
    assert heads[:3] == list(R.B_CHAPTERS[:3]) and heads[3].startswith(R.B_CHAPTERS[3]) and heads[4:] == list(R.B_CHAPTERS[4:])
    for sub in R.B_DETAIL:  # 필수 5항목(사업 아이디어·시장 규모·팀의 구성·사업 리스크)은 최고점 후보 상세에, 한계점은 5장
        assert f"### {sub}" in md
    evals = state["evaluations"]
    eligible = sum(bool(r.get("eligible")) for r in state["screened"])
    lines = _summary(md)
    assert len(lines) == 5
    assert lines[1].startswith(f"- 결론: 투자 추천 없음 — 평가한 {len(evals)}곳 모두 보류(적격 {eligible}곳 중 비용 상한 {len(evals)}곳 평가)")
    assert "최고점 메타파머스 동종 평균 대비 113.0(기준 130)" in lines[1]
    # 보류 유형은 새 우선순위(Deal-killer > 창업자 점수 평균 미만 > 동종 평균 이하)를 따른다 (픽스처에 Deal-killer 없음)
    assert all(e["hold_type"] == ("창업자 점수 평균 미만" if e["scorecard"]["founder_c"] < 1.00 else "동종 평균 이하")
               for e in evals)
    assert lines[2].startswith("- 원인: 보류 유형 동종 평균 이하 6 · 창업자 점수 평균 미만 4 — ")
    assert "동종 평균 130" not in md  # 시나리오 기준 1.30 은 '기준 130'이지 동종 평균이 아니다
    # 3장 첫 줄: 배수 = 기준이어도 보류(투자 ⇔ 배수 > 기준). 배수 표기는 정수·소수 한 자리 모두 허용(표기 변경 중)
    assert re.search(r"평가한 10곳 중 최고점\(동종 평균 대비 113(\.0)?\)이지만 기준 130을 넘지 못했다\.", md)
    assert lines[4].startswith("- 요청: 메타파머스 실사 착수 또는 미평가 적격") and lines[4].endswith("할까요?")
    ch2 = md.split("## 2. 후보별 보류 사유", 1)[1].split("\n## ", 1)[0]
    blocks = ch2.split("\n### ")[1:]
    assert len(blocks) == len(evals)
    from agents.decision import eval_rank_key   # 배수 동점(리비타·바르카 0.9562)은 창업자 점수 순 — 평가 순서와 무관
    for b, e in zip(blocks, sorted(evals, key=eval_rank_key)):
        assert e["name"] in b.splitlines()[0] and e["hold_type"] in b.splitlines()[0]
        assert "- 뒤집힘 조건: " in b and "- 왜 안 되는가: " in b and "- 팀: " in b and "- 사업: " in b
        if e["flip"] and e["flip"].get("new_multiplier"):
            assert f"{e['flip']['new_multiplier'] * 100:.1f}" in b
    for key in ("chapter_refs_ok", "required_items_ok", "refs_match_citations", "reference_format_ok",
                "conclusion_matches_scorecard", "assumptions_labeled"):
        assert ck[key] is True, (key, ck)
    assert ck["banned_hits"] == [] and ck["consistency_violations"] == []
    if can_render:
        assert rep["pages"] <= 5 and rep["summary_ratio"] <= 0.5, (rep["pages"], rep["summary_ratio"], ck["density_level"])


def test_hold_candidate_claims_checked_per_candidate(run):
    """후보별 '왜 안 되는가'는 그 후보의 평가표와 대조한다 (리비타 특허 미확인인데 '특허를 보유' → 재작성 요청)."""
    bad = copy.deepcopy(HOLD)
    bad["candidates"] = [R.CandidateNote(name="리비타", business="", why_not="리비타는 특허를 보유했지만 매출 규모는 공개 근거가 없다 [W903d5].")
                         if c.name == "리비타" else c for c in bad["candidates"]]
    _, _, llm = run(_load("hold"), [bad, HOLD])
    assert len(llm.prompts) == 2 and "[리비타] '특허를 보유'" in llm.prompts[1]


# ── C안·보정 실행

def test_no_candidate_report(run):
    state = _load("hold")
    state = {**state, "evaluations": [], "screened": [dict(r, eligible=False, reason="G2 투자 단계 확인 불가") for r in state["screened"]]}
    rep, md, llm = run(state, [HOLD])
    assert rep["mode"] == "none" and not llm.prompts  # LLM 없이 코드로 작성
    assert _heads(md) == list(R.C_CHAPTERS)
    assert _summary(md)[1] == "- 결론: 투자 추천 대상 없음 — 적격 후보 0곳"
    assert rep["checks"]["chapter_refs_ok"] and rep["checks"]["refs_match_citations"]


def test_calibrate_mode_is_noop(run, set_cfg, tmp_path):
    set_cfg("workflow.calibrate", True)
    rep, md, llm = run(_load("invest"), [INVEST])
    assert rep["mode"] == "calibrate" and rep["pdf"] is None and rep["submit_pdf"] is None and not llm.prompts
    assert not (tmp_path / "out").exists()


# ── 검사 함수·REFERENCE 표기 단위 테스트

def test_checks_detect_bad_chapter_ref_and_citation_mismatch():
    view = {"summary": [["상황", "a"], ["결론", "X 투자 추천"], ["근거", "b (→9장)"]]}
    md = "# t\n\n## SUMMARY\n- 근거: b (→9장)\n\n## 1. 사업 아이디어\n\n본문 [1, 2]\n\n## REFERENCE\n\n### 웹페이지\n1. x\n"
    refs = [{"n": 1, "group": "웹페이지", "text": "홍길동(2026-01-01). 제목. 사이트, https://a.b/c"}]
    ck = R._checks(md, view, refs, "invest", {"conclusion_ok": lambda c: "투자 추천" in c}, [], 560)
    assert ck["chapter_refs_ok"] is False and ck["refs_match_citations"] is False and ck["required_items_ok"] is False
    assert ck["conclusion_matches_scorecard"] is True and ck["reference_format_ok"] is True


def test_format_reference_access_date_without_note():
    reg = SourceRegistry()
    sid = reg.add_web({"url": "https://example.com/news/1", "title": "메타파머스 로봇 공개 - 예시일보", "content": "본문"},
                      "tech", "q", "2026-09-30")
    s = reg.get(sid)
    assert s["date"] is None and uses_access_date(s)
    line = format_reference(s)
    # 바이라인이 없는 기사: 작성자 자리에 매체명을 쓰지 않는다 (매체명은 사이트 자리에만)
    assert line.startswith("작성자 미표기(2026-09-30). 메타파머스 로봇 공개. 예시일보, https://example.com/news/1")
    assert not line.endswith(")") and REFERENCE_FORMATS["웹페이지"].match(line)
    tips = reg.add_web({"url": "https://jointips.or.kr/network/startups", "title": "TIPS 창업기업: 메타파머스",
                        "content": "설립일 2022-09-02", "published_date": "2022-09-02"}, "discovery", "q", "2026-09-30",
                       key="tips:메타파머스")
    t = reg.get(tips)
    assert uses_access_date(t) and "(2026-09-30)" in format_reference(t) and "2022-09-02" not in format_reference(t)
    doc = {"kind": "doc", "type": "report", "publisher": "AgFunder", "year": 2026, "title": "Global AgriFoodTech Investment Report 2026",
           "url": "https://agfunder.com/research/x/"}
    assert REFERENCE_FORMATS["기관 보고서"].match(format_reference(doc)) and not uses_access_date(doc)


def test_flip_text_deal_killer_and_note():
    # 문항이 없거나 Deal-killer 면 투자 판단 에이전트의 note 를 그대로, 문항이 있으면 구조화 필드로 문장을 만든다
    assert R._flip_text({"flip": {"note": "미확인 문항이 없어 뒤집힘 조건 없음", "items": [], "new_multiplier": 1.0}}) \
        == "미확인 문항이 없어 뒤집힘 조건 없음"
    assert R._flip_text({"flip": {"note": "K1 해소 필요(x)", "items": ["P4 x"], "new_multiplier": None}}) == "K1 해소 필요(x)"
    assert R._flip_text({"flip": None, "scorecard": {"deal_killers": ["K1"]}}).startswith("Deal-killer K1 해소 필요")
    sc = {"threshold": 1.1}
    assert R._flip_text({"flip": {"items": ["C1 특허"], "new_multiplier": 1.123, "reached": True}, "scorecard": sc}) \
        == "C1 특허 이(가) YES 로 확인되면 동종 평균 대비 112.3(기준 110) → 투자 조건 충족"
    assert R._flip_text({"flip": {"items": ["C1 특허"], "new_multiplier": 1.05, "reached": False}, "scorecard": sc}) \
        == "C1 특허 이(가) 모두 YES 로 확인돼도 동종 평균 대비 105.0(기준 110) — 기준 미달"


def test_pct_not_double_scaled_and_assumption_scope():
    sc = {"rows": [], "multiplier": 1.13, "threshold": 1.1,
          "criteria": [{"name": "창업자·경영진", "dim": "founder", "weight": 30, "yes": 3, "no": 0, "unknown": 1,
                        "pct": 129.2, "contribution": 0.3876}]}
    assert R._criteria_rows(sc)[0][2] == "129.2"   # 이미 ×100 된 값을 다시 곱하지 않고, 소수 한 자리로
    md = "## 2. 시장\n원화 환율 상승이 수입 자재 가격을 올렸다\n**ROI 참고치 (점수·결정에 넣지 않음)**\n\n- 환율: 1,400원\n\n## 5. 한계점\n- 지분율 20%\n"
    assert R._assumption_scope(md) == ["**ROI 참고치 (점수·결정에 넣지 않음)**", "", "- 환율: 1,400원", "", "## 5. 한계점", "- 지분율 20%"]


# ── 보고서 사실 불일치 수정 (퍼널·시장 규모·대상과 차점 후보·캡션·제품명·인물 표·REFERENCE·숫자 대조)

def test_funnel_shows_screened_count():
    """관문은 발굴한 전부가 아니라 라운드당 검증 상한만 검사한다 → '발굴 → 관문 검사 → 적격'."""
    assert R._funnel({"discovered": 78, "screened": 18, "eligible": 14}) == \
        "국내외 AgTech AI 스타트업 78곳 발굴 → 관문 검사 18곳 → 적격 14곳(모두 Seed~Series C 투자 유치 확인)"


def test_market_level_and_same_estimate_cagr():
    from core.config import get_segment

    assert R._market_level("글로벌 스마트 농업 시장", get_segment("robotics")) == "parent"
    assert R._market_level("글로벌 농업 로봇 시장", get_segment("robotics")) == "segment"
    assert R._market_level("국내 스마트팜 시장", get_segment("greenhouse")) == "segment"
    table = "BIS Research\n2023~2034\n174억 달러~1,172억 달러\n19.1%\nGrand View Research\n2023~2030\n226억 5천만 달러~556억 4천만 달러\n13.7%"
    assert R._estimate_near(table, "2023년 226억 5천만 달러") == {"end": "556억 4천만 달러", "end_year": "2030",
                                                                "period": "2023~2030", "cagr": 13.7}


def test_cagr_check_catches_size_paired_with_other_estimate_growth():
    """226.5억 → 556.4억 달러(2023→2030)는 연평균 약 13.7% 인데 다른 추정의 19.1% 를 같은 목록에 붙인 경우."""
    md = ("## 2. 시장 규모와 성장성\n\n**결론**\n\n2023년 기준 226억 5천만 달러에서 2030년 556억 4천만 달러로 성장 예상 [3]\n\n"
          "- 세부 시장 규모: 2023년 226억 5천만 달러 (글로벌 스마트 농업 시장) [3]\n- 성장률: 연평균 19.1% (2024) [3]\n\n## 3. 팀\n")
    probs = R._cagr_problems(R._market_units(md))
    assert len(probs) == 1 and "약 13.7%" in probs[0] and "'19.1%'" in probs[0]
    fixed = md.replace("- 성장률: 연평균 19.1% (2024) [3]", "- 성장률(다른 추정): 연평균 19.1% (2024~2034 예측) [3]")
    assert R._cagr_problems(R._market_units(fixed)) == []


def _audit_reg():
    reg = SourceRegistry()
    d = reg.add_doc({"doc_id": "x", "page": 3, "type": "report", "publisher": "기관", "year": 2024, "title": "t"},
                    "BIS Research는 2023년 174억 달러에서 2034년 1,172억 달러로 연평균 19.1% 성장을 예상했다. "
                    "Grand View Research는 2023년 226억 5천만 달러로 평가했다.", "market", "q")
    w = reg.add_web({"url": "https://a.kr/1", "title": "메타파머스 투자 유치", "published_date": "2025-08-12",
                     "content": "메타파머스가 30억원 규모 프리A 투자를 유치했다. 직원은 seven employees 다."}, "tech", "q", "2026-09-30")
    return reg, d, w


def test_number_audit_flags_numbers_not_in_input_or_citation():
    reg, d, w = _audit_reg()
    draft = R.Draft(**dict(INVEST, market=f"글로벌 시장은 2023년 174억 달러에서 2034년 1,172억 달러로 연평균 19.1% 성장 전망 [{d}]; "
                                          f"2023년 기준 226억 5천만 달러로도 추정된다 [{d}]. 국내는 연평균 47.3% 성장한다 [{d}].",
                           team=f"프리A 30억원을 유치했다 [{d}]. 직원 7명이다 [{w}]."))
    a = R._number_audit(draft, {"evidence": reg.brief([d, w])}, reg)
    joined = "\n".join(a["unsupported"])
    assert "[2장 시장] '47.3%' 입력에 없음" in joined                       # 근거·점수표 어디에도 없는 숫자
    assert "[3장 팀] '30억원' 인용 근거에 없음" in joined                    # 입력에는 있지만 붙인 인용 근거에는 없음
    assert "174억 달러" not in joined and "'7명'" not in joined              # 근거에 있는 숫자 (영어 'seven employees' 포함)
    assert any("같은 2023년 규모가 174억 달러 · 226억 5천만 달러" in c for c in a["conflicts"])
    note = R._multi_estimate_note(draft.market, reg)
    assert "174억 달러(BIS Research 추정)" in note and "226억 5천만 달러(Grand View Research 추정)" in note


def test_number_audit_ignores_rule_window_but_checks_other_months():
    """'최근 24개월'·'24개월 이상 경과'는 평가 규칙이라 근거와 대조하지 않는다. 회사 사실로 쓴 개월 수는 그대로 대조한다
    (이전에는 '24개월'을 개수 '24개'로 읽어 보류 사유마다 '인용 근거에 없음' 경고가 났다 — 2026-10-12 보류 시나리오)."""
    reg, d, w = _audit_reg()
    draft = R.Draft(**dict(INVEST, team=f"최근 라운드가 24개월 이상 경과했고 최근 24개월 마일스톤 증빙 부족 [{w}]. "
                                       f"설립 18개월 만에 투자를 유치했다 [{w}]."))
    joined = "\n".join(R._number_audit(draft, {"evidence": reg.brief([d, w])}, reg)["unsupported"])
    assert "24개" not in joined and "'18개월' 입력에 없음" in joined


def test_number_rewrite_off_and_once_when_on(run, set_cfg):
    """숫자 대조는 끄면 '검사만'(LLM 호출 그대로). 켜면 1회만 다시 쓰고, 경고가 줄면 받아들인다."""
    state = _load("invest")
    bad = dict(INVEST, lead_market="국내 스마트농업 시장은 연평균 47.3% 성장한다.")
    set_cfg("report.number_check", {"rewrite": False})
    rep, md, llm = run(state, [bad])
    assert len(llm.prompts) == 1 and "47.3%" in md
    audit = rep["checks"]["number_audit"]
    assert audit["rewrite"].startswith("off") and any("'47.3%' 입력에 없음" in x for x in audit["unsupported"])
    set_cfg("report.number_check", {"rewrite": True})
    rep2, md2, llm2 = run(state, [bad, INVEST])
    assert len(llm2.prompts) == 2 and "47.3%" in llm2.prompts[1] and "47.3%" not in md2
    assert rep2["checks"]["rewrites"] == 1


def test_number_rewrite_triggers_on_mixed_estimates(run, set_cfg):
    """서로 다른 기관 추정을 한 문장에 이은 경우(226.5억→1,172억 달러는 11년 연평균 약 16.1% 인데 19.1%)도 다시 쓰기 대상이다."""
    mixed = "글로벌 스마트 농업 시장은 2023년 226억 5천만 달러에서 2034년 1,172억 달러로 연평균 19.1% 성장 전망이다."
    assert R._draft_cagr(SimpleNamespace(lead_market="", market=mixed))
    assert R._draft_cagr(SimpleNamespace(market=mixed.replace("19.1%", "16.1%"))) == []
    set_cfg("report.number_check", {"rewrite": True})
    rep, md, llm = run(_load("invest"), [dict(INVEST, market=mixed + " [D72ffc]"), INVEST])
    fb = llm.prompts[1][llm.prompts[1].find("직전 초안의 다음 숫자"):][:2000]
    assert len(llm.prompts) == 2 and "연평균은 약 16.1%" in fb and "기관별로 나눠" in fb, fb
    assert "1,172억 달러로 연평균 19.1%" not in md and rep["checks"]["rewrites"] == 1


def test_enforce_cagr_replaces_mixed_market_sentences_in_code():
    """다시 쓰기 뒤에도 남은 혼합 문장(Grand View 2023 값 + BIS 2034 값·연평균)은 코드가 빼고,
    근거 원문에서 같은 추정의 끝 값·연평균으로 만든 대표 수치 문장으로 바꾼다 (LLM 호출 없음)."""
    reg = SourceRegistry()
    table = "BIS Research 2023~2034 174억 달러~1,172억 달러 19.1% Grand View Research 2023~2030 226억 5천만 달러~556억 4천만 달러 13.7%"
    sid = reg.add_doc({"doc_id": "krei", "page": 4, "type": "paper", "publisher": "농촌경제연구원", "year": 2024, "title": "t"},
                      table, "market", "q")
    target = {"market": {"size": {"raw": "2023년 226억 5천만 달러", "scope": "글로벌 스마트 농업 시장", "source_id": sid}}}
    mixed = "글로벌 스마트 농업 시장은 2023년 226억 5천만 달러에서 2034년 1,172억 달러로 연평균 19.1% 성장 전망이다."
    draft = R.Draft.model_validate(dict(INVEST, ev_market=mixed, lead_market=mixed,
                                        market=mixed + " 국내는 자율주행 농기계 보급을 확대하고 있다 [D72ffc]."))
    fixed, removed = R._enforce_cagr(draft, target, reg)
    rep = "글로벌 스마트 농업 시장 규모 2023년 226억 5천만 달러 → 2030년 556억 4천만 달러, 연평균 13.7%"
    assert R._draft_cagr(draft) and R._draft_cagr(fixed) == [] and len(removed) == 3
    assert fixed.ev_market == f"{rep} [{sid}]" and fixed.lead_market == rep
    assert fixed.market == "국내는 자율주행 농기계 보급을 확대하고 있다 [D72ffc]."   # 맞는 문장은 남긴다
    assert R._enforce_cagr(fixed, target, reg) == (fixed, [])                    # 고칠 것이 없으면 그대로


def test_name_variants_with_source_dates():
    reg = SourceRegistry()
    a = reg.add_web({"url": "https://a.kr/1", "title": "수확 로봇", "published_date": "2023-11-12",
                     "content": "메타파머스는 수확 로봇 메타파머를 개발했다"}, "tech", "q", "2026-09-30")
    b = reg.add_web({"url": "https://b.kr/2", "title": "범용 로봇", "published_date": "2025-08-12",
                     "content": "메타파머스의 범용 로봇 옴니파머(Omni Farmer)"}, "tech", "q", "2026-09-30")
    target = {"name": "메타파머스", "profile": {"name": "메타파머스", "evidence_ids": [a, b]}}
    v = R._name_variants(["농작업 로봇 '옴니파머(Omni Farmer)'와 원격제어 소프트웨어 '탭파머스'", "농업 로봇 '메타파머'"], target, reg)
    assert v == [{"kind": "하드웨어", "names": {"옴니파머": {"ids": [b], "from": "2025-08-12", "to": "2025-08-12"},
                                              "메타파머": {"ids": [a], "from": "2023-11-12", "to": "2023-11-12"}}}]
    assert R._name_note(v).startswith("제품명 표기: ‘메타파머’(2023-11~2023-11 근거 1건) / ‘옴니파머’(2025-08~2025-08 근거 1건)")


def test_person_unknown_background_uses_scorecard_quote():
    reg = SourceRegistry()
    w = reg.add_web({"url": "https://z.kr/1", "title": "인터뷰", "content": "윤원재 CTO를 비롯한 공동창업자들은 AI 로봇을 전공했다"},
                    "founder", "q", "2026-09-30")
    quote = "윤원재 CTO를 비롯한 공동창업자들은 AI 로봇을 전공했다"
    target = {"profile": {}, "scorecard": {"rows": [{"qid": "F2", "dim": "founder", "answer": "YES", "quote": quote,
                                                     "evidence_ids": [w]}]},
              "founder": {"people": [{"name": "윤원재", "role": "기술이사(CTO)", "background": "확인 불가", "evidence_ids": []}]}}
    x = target["founder"]["people"][0]
    assert R._person_cell(x, target, reg, []) == f"평가표 F2 원문: “{quote}” [{w}]"
    assert R._team_conflicts(["CTO 등 AI·로봇 전문 인력 포함"], target, reg) == []
    target["scorecard"]["rows"] = []   # 원문 문장을 못 찾으면 표는 '확인 불가' → 본문이 CTO 역량을 쓰면 경고
    assert R._team_conflicts(["CTO 등 AI·로봇 전문 인력 포함"], target, reg)


def test_head_to_head_lines(tmp_path, set_cfg):
    from agents.decision import _qids, load_rubric

    evals = _load("hold")["evaluations"]
    members = [{"name": e["name"], "region": e["region"], "stage": e["stage"], "segment_id": e["segment_id"],
                "signals": {r["qid"]: r["x"] for r in e["scorecard"]["rows"]}} for e in evals]
    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps({"version": 1, "run_date": "2026-09-30", "rubric_qids": _qids(load_rubric()), "n": len(members),
                               "members": members, "mean": {}}, ensure_ascii=False), encoding="utf-8")
    set_cfg("decision.reference_file", str(ref))
    ranked = sorted(evals, key=lambda e: -e["scorecard"]["multiplier"])
    lines = R._head_to_head(ranked[0], ranked)
    assert len(lines) == 3
    assert lines[0].startswith(f"{ranked[0]['name']} {ranked[0]['scorecard']['multiplier'] * 100:.1f} 대 {ranked[1]['name']}")
    assert "앞서는 조건" in lines[1] or "넘지 못한다" in lines[1]
    assert "밀리는 조건" in lines[2] or "보다 높다" in lines[2]


def test_reference_institutional_serial_portal_and_byline():
    krei = {"kind": "doc", "type": "paper", "publisher": "한국농촌경제연구원", "year": 2024, "authors": "박미선", "journal": "세계농업",
            "volume": 2024, "issue": "겨울호", "pages": "33-48", "title": "스마트 농업과 농업 부문 고용 현황 및 전망",
            "url": "https://www.krei.re.kr/x"}
    assert reference_group(krei) == "기관 보고서"   # 연구기관 정기간행물 기고는 학술 논문이 아니다
    line = format_reference(krei)
    assert line == ("한국농촌경제연구원(2024). 스마트 농업과 농업 부문 고용 현황 및 전망(박미선, 『세계농업』 2024 겨울호, 33-48쪽). "
                    "https://www.krei.re.kr/x") and REFERENCE_FORMATS["기관 보고서"].match(line)
    assert reference_group(dict(krei, journal="Robotics", publisher="MDPI")) == "학술 논문"
    reg = SourceRegistry()
    z = reg.add_web({"url": "https://zdnet.co.kr/view?no=1", "title": "메타파머스 인터뷰", "content": "요약",
                     "published_date": "2024-10-16",
                     "raw_content": "입력 :2024/10/16 20:45 방은주 기자 기자 페이지 … 방은주 기자ejbang@zdnet.co.kr 다른 기사 김철수 기자"},
                    "founder", "q", "2026-09-30")
    assert format_reference(reg.get(z)).startswith("방은주 기자(2024-10-16). ")   # 스니펫에 없던 바이라인을 본문에서
    d = reg.add_web({"url": "https://v.daum.net/v/abc", "title": "로봇농부가 온다", "published_date": "2023-11-12",
                     "content": "최효정 기자 … Copyright ⓒ 조선비즈 & Chosun.com"}, "founder", "q", "2026-09-30")
    assert format_reference(reg.get(d)) == ("최효정 기자(2023-11-12). 로봇농부가 온다. 조선비즈(포털 전재본), "
                                            "https://v.daum.net/v/abc")


def test_table_captions_do_not_repeat_chapter_numbers(run, can_render):
    rep, md, _ = run(_load("invest"), [INVEST])
    html = Path(rep["html"]).read_text(encoding="utf-8")
    caps = re.findall(r'<div class="cap"><b>표 \d+\.</b>([^<]*)</div>', html)
    assert caps and not [c for c in caps if re.match(r"\s*\d", c)], caps
