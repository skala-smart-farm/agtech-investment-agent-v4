"""v3 경쟁사 실재 확인 검사 (평가표 C2 '국내외 경쟁사 2곳 이상과 비교'). 키·네트워크 없이 돈다.

- 출처 도메인: 하위 호스트·국가 도메인 묶기, 같은 제목 기사(포털 전재본)는 한 곳으로
- 이름 키: 나라 이름·법인 접미사만으로 근거가 맞지 않게
- 같은 세부 분야 적격 후보(state['screened'])를 경쟁사 후보로 먼저 넣고 '확인'으로 둔다 (검색 없음)
- 웹에서 찾은 경쟁사는 서로 다른 도메인 2곳 이상(그중 1곳은 대상 회사 기사 밖)일 때만 '확인', 모자라면 회사명 검색
  (경쟁사 1곳당 1회, 평가 후보 1곳당 competition.verify_max_searches 회까지). 그래도 모자라면 '근거 약함'
- core.judge: 확인된 경쟁사가 rubric C2 min_competitors(2) 미만이면 C2 의 YES → UNKNOWN
- 보고서 경쟁사 표: '출처 수' 칸(도메인 수·확인/근거 약함), 확인된 경쟁사를 위로
"""
from __future__ import annotations

import pytest

import agents.competition as comp
import agents.report as R
import core.judge as judge
from core.judge import Answer, Answers, judge_company, judge_dimension, load_rubric
from tools.sources import SourceRegistry, naming_domains, source_domain

RUN_DATE = "2026-09-29"


def _web(reg: SourceRegistry, url: str, title: str, content: str, agent: str = "eligibility") -> str:
    return reg.add_web({"url": url, "title": title, "content": content}, agent, "q", RUN_DATE)


# ── 출처 도메인·이름 키
def test_source_domain_groups_subdomains_and_country_domains():
    web = lambda u: {"kind": "web", "url": u}  # noqa: E731
    assert source_domain(web("https://n.news.naver.com/article/1")) == "naver.com"
    assert source_domain(web("https://news.naver.com/main/2")) == "naver.com"
    assert source_domain(web("https://www.mk.co.kr/news/1")) == "mk.co.kr"
    assert source_domain(web("https://www.bbc.co.uk/news/x")) == "bbc.co.uk"
    assert source_domain(web("https://agfundernews.com/x")) == "agfundernews.com"
    assert source_domain({"kind": "doc", "publisher": "농촌진흥청"}) == "doc:농촌진흥청"


def test_naming_domains_counts_same_article_once():
    reg = SourceRegistry()
    a = _web(reg, "https://news-a.com/1", "로보팜, 토마토 수확 로봇 첫 공개", "로보팜이 토마토 수확 로봇을 공개했다")
    b = _web(reg, "https://v.daum.net/v/2", "로보팜, 토마토 수확 로봇 첫 공개", "로보팜이 토마토 수확 로봇을 공개했다")  # 포털 전재본
    c = _web(reg, "https://www.farm-b.co.kr/3", "수확 로봇 업체 탐방", "로보팜 공장을 찾았다")
    d = _web(reg, "https://news-c.com/4", "스마트팜 동향", "다른 회사 이야기")
    doms = naming_domains(reg, [a, b, c, d], ["로보팜"])
    assert doms == {"news-a.com": [a, b], "farm-b.co.kr": [c]}


def test_rival_keys_drop_country_and_legal_suffix():
    assert comp.rival_keys("더그투스(Dogtooth Technologies)") == ["더그투스", "dogtoothtechnologies", "dogtooth"]
    assert comp.rival_keys("Tevel (이스라엘)") == ["tevel"]          # '이스라엘'이 키가 되면 아무 해외 기사에나 맞는다
    assert comp.rival_keys("Dogtooth, Inc.") == ["dogtooth"]
    assert comp.rival_keys("㈜트랙봇") == ["트랙봇"]


def test_verify_query_language_by_country():
    plan = comp.SearchPlan(product_type_ko="과채류 수확 로봇", product_type_en="fruit harvesting robot", queries=[])
    assert comp._verify_query({"name": "로보팜", "name_en": "Robofarm", "country": "한국"}, plan) == '"로보팜" 과채류 수확 로봇'
    assert comp._verify_query({"name": "더그투스", "name_en": "Dogtooth Technologies", "country": "영국"}, plan) \
        == '"Dogtooth" fruit harvesting robot'
    assert comp._verify_query({"name": "테벨", "name_en": "", "country": "이스라엘"}, plan) == '"테벨" 과채류 수확 로봇'


# ── 같은 세부 분야 적격 후보
def _screened(reg: SourceRegistry) -> tuple[list[dict], dict]:
    ids = {
        "own": _web(reg, "https://www.trackbot.co.kr/about", "트랙봇 회사 소개", "트랙봇은 딸기 수확 로봇을 만든다"),
        "news1": _web(reg, "https://news-one.com/a", "트랙봇, 시드 투자 유치", "트랙봇이 딸기 수확 로봇으로 시드 투자를 받았다"),
        "news2": _web(reg, "https://n.news.naver.com/b", "트랙봇 수확 로봇 농가 보급", "트랙봇 로봇이 농가에 들어간다"),
        "news3": _web(reg, "https://news.naver.com/c", "트랙봇 대표 인터뷰", "트랙봇 대표를 만났다"),   # naver.com 두 번째
        "other": _web(reg, "https://news-two.com/d", "탈락봇 소식", "탈락봇 이야기"),
        "green": _web(reg, "https://news-three.com/e", "온실팜 소식", "온실팜 이야기"),
        "up": _web(reg, "https://agfundernews.com/up", "Upside Robotics raises seed", "Upside Robotics builds field robots"),
    }
    rec = lambda **kw: {"region": "KR", "segment_id": "robotics", "eligible": True, "one_line": "", **kw}  # noqa: E731
    screened = [
        rec(name="테스트로봇", official_name="테스트로봇", evidence_ids=[]),                         # 대상 자신
        rec(name="트랙봇", official_name="트랙봇", name_en="Trackbot", one_line="딸기 수확 로봇",
            evidence_ids=[ids["own"], ids["news1"], ids["news2"], ids["news3"]]),
        rec(name="탈락봇", eligible=False, evidence_ids=[ids["other"]]),                             # 관문 탈락
        rec(name="온실팜", segment_id="greenhouse", evidence_ids=[ids["green"]]),                    # 다른 분야
        rec(name="Upside Robotics", region="GLOBAL", segment_id="robotics, greenhouse",            # 여러 id → 첫 유효 id
            evidence_ids=[ids["up"]]),
        rec(name="근거없는봇", evidence_ids=[ids["other"]]),                                          # 이름이 나오는 근거 없음
    ]
    return screened, ids


def target(**kw) -> dict:
    c = {"name": "테스트로봇", "official_name": "테스트로봇", "name_en": "Testrobot", "region": "KR",
         "segment_id": "robotics", "stage": "Seed", "round_date": "2025-05", "round_amount": "10억 원",
         "founded_year": 2020, "one_line": "과채류 수확 로봇", "evidence_ids": []}
    c.update(kw)
    return c


def test_same_segment_peers_filters_and_picks_third_party_sources():
    reg = SourceRegistry()
    screened, ids = _screened(reg)
    peers = comp.same_segment_peers({"current": target(), "screened": screened}, reg, limit=5)
    assert [p["name"] for p in peers] == ["트랙봇", "Upside Robotics"]
    tb = peers[0]
    assert tb["country"] == "한국" and tb["domains"] == 3                 # trackbot.co.kr · news-one.com · naver.com
    assert tb["evidence_ids"] == [ids["news1"], ids["news2"]]              # 제3자 기사 먼저, 도메인마다 1개, 최대 2개
    assert comp.same_segment_peers({"current": target(), "screened": screened}, reg, limit=1) == peers[:1]
    assert comp.same_segment_peers({"current": target(segment_id=None), "screened": screened}, reg, limit=5) == []


# ── 경쟁사 노드 (가짜 LLM·가짜 검색)
class FakeLLM:
    def __init__(self, answers: dict[str, list]):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, schema, role: str = "generator", kind: str = "default"):
        llm = self

        class _Bound:
            def invoke(self, prompt: str):
                llm.prompts.append((schema.__name__, prompt))
                return llm.answers[schema.__name__].pop(0)
        return _Bound()


class FakeWeb:
    def __init__(self, results: dict[str, list[dict]]):
        self.results, self.calls = results, []

    def __call__(self, query, reg, agent, **kw):
        self.calls.append(query)
        return [reg.add_web(r, agent, query, RUN_DATE) for r in self.results.get(query, [])]


PLAN = comp.SearchPlan(product_type_ko="과채류 수확 로봇", product_type_en="fruit harvesting robot",
                       queries=["국내 수확 로봇 스타트업", "fruit harvesting robot startup"])
WEB = {
    "국내 수확 로봇 스타트업": [{"url": "https://www.agnews.kr/r1", "title": "수확 로봇 스타트업 비교",
                                 "content": "로보팜은 토마토 수확 로봇을 만든다"}],
    "fruit harvesting robot startup": [{"url": "https://techsite.com/tevel", "title": "Flying fruit pickers",
                                        "content": "Tevel builds flying fruit picking robots"}],
    "테스트로봇 경쟁사": [  # 대상 회사 기사 두 곳에만 나오는 경쟁사 (더그투스)
        {"url": "https://dailyfarm.co.kr/a", "title": "테스트로봇, 수확 로봇 공개",
         "content": "테스트로봇은 영국 더그투스 등 외국 경쟁사와 겨룬다"},
        {"url": "https://agrinews.com/b", "title": "테스트로봇 대표 인터뷰",
         "content": "테스트로봇 대표는 더그투스 같은 경쟁사를 언급했다"}],
    '"Tevel" fruit harvesting robot': [  # 실재 확인 검색: 대상 회사 기사 밖 독립 출처 2곳
        {"url": "https://www.tevel-tech.com/", "title": "Tevel Aerobotics", "content": "Tevel flying autonomous robots"},
        {"url": "https://www.reuters.com/tevel", "title": "Tevel raises funding", "content": "Tevel raised money"}],
}


def _analysis(ids: dict) -> comp.CompetitionAnalysis:
    C = comp.Competitor
    rows = [C(name="더그투스", name_en="Dogtooth Technologies", country="영국", offering="딸기 수확 로봇", scale="확인 불가",
              vs_target="경쟁사: 딸기 수확 / 대상: 회사 측 주장: 범용", evidence_ids=[ids["dog"]]),
            C(name="Tevel", name_en="Tevel Aerobotics Technologies", country="이스라엘", offering="비행 수확 로봇",
              scale="확인 불가", vs_target="경쟁사: 비행 로봇 / 대상: 회사 측 주장: 지상 로봇", evidence_ids=[ids["tevel"]]),
            C(name="트랙봇", name_en="Trackbot", country="한국", offering="딸기 수확 로봇", scale="시드",
              vs_target="경쟁사: 딸기 수확 / 대상: 회사 측 주장: 과채류 전반", evidence_ids=[ids["news1"]]),
            C(name="로보팜", name_en="", country="한국", offering="토마토 수확 로봇", scale="확인 불가",
              vs_target="경쟁사: 토마토 / 대상: 회사 측 주장: 딸기·토마토", evidence_ids=[ids["robo"]]),
            C(name="지어낸로봇", name_en="", country="한국", offering="-", scale="-", vs_target="-", evidence_ids=[])]
    return comp.CompetitionAnalysis(competitors=rows, differentiation="회사 측 주장: 범용", entry_barriers="약함",
                                    threats=["해외 업체"], verified_claims=[], evidence_ids=[])


@pytest.fixture
def judged(monkeypatch):
    calls = []

    def fake_judge(dim_id, company, pool_ids, reg, analysis, run_date):
        calls.append({"dim": dim_id, "company": company, "pool": list(pool_ids), "analysis": analysis})
        return {"dim": dim_id, "name": dim_id, "weight": 10, "owner": "competition", "rows": [], "yes": 0, "no": 0,
                "unknown": 4, "na": 0, "n": 4, "rejected_yes": [], "quote_retried": 0}
    monkeypatch.setattr(judge, "judge_dimension", fake_judge)
    return calls


def _run(monkeypatch, judged):
    reg = SourceRegistry()
    screened, ids = _screened(reg)
    ids |= {"dog": SourceRegistry._id("W", "web:https://dailyfarm.co.kr/a"),
            "tevel": SourceRegistry._id("W", "web:https://techsite.com/tevel"),
            "robo": SourceRegistry._id("W", "web:https://www.agnews.kr/r1")}
    web = FakeWeb(WEB)
    llm = FakeLLM({"SearchPlan": [PLAN], "CompetitionAnalysis": [_analysis(ids)]})
    monkeypatch.setattr(comp, "web_search", web)
    monkeypatch.setattr(comp, "structured", llm)
    monkeypatch.setattr(comp, "bounded", llm)
    state = {"current": target(), "registry": reg.data, "run_date": RUN_DATE, "screened": screened,
             "founder": {}, "tech": {"product": "과채류 수확 로봇", "core_technology": "비전", "claims": [], "pool_ids": [],
                                     "evidence_ids": []}, "market": {}, "competition": {}}
    out = comp.competition_node(state)
    return out, web, llm, ids


def test_competition_peers_first_and_domain_check(monkeypatch, judged):
    out, web, llm, ids = _run(monkeypatch, judged)
    k = out["competition"]
    prompt = next(p for s, p in llm.prompts if s == "CompetitionAnalysis")
    # 같은 분야 적격 후보가 프롬프트에 먼저 들어간다 (대상 자신·관문 탈락·다른 분야는 빠짐)
    assert f"- 트랙봇 (한국) [{ids['news1']}, {ids['news2']}]: 딸기 수확 로봇" in prompt
    assert "탈락봇" not in prompt and "온실팜" not in prompt and "- 테스트로봇 (" not in prompt
    assert f"[{ids['news1']}] (웹, " in prompt                    # 후보의 관문 근거 요약이 근거에 붙는다

    rows = {r["name"]: r for r in k["competitors"]}
    assert "지어낸로봇" not in rows                                # 근거에 이름이 없는 회사는 버린다
    assert [r["name"] for r in k["competitors"]] == ["Tevel", "트랙봇", "더그투스", "로보팜"]   # 확인된 경쟁사가 앞
    assert (rows["트랙봇"]["verified"], rows["트랙봇"]["peer"], rows["트랙봇"]["n_sources"]) == ("확인", True, 3)
    assert rows["트랙봇"]["source_ids"] == [ids["news1"], ids["news2"], ids["own"]]   # 회사 자체 사이트는 뒤로
    assert (rows["Tevel"]["verified"], rows["Tevel"]["n_sources"]) == ("확인", 3)   # techsite + 실재 확인 검색 2곳
    assert rows["더그투스"]["verified"] == "근거 약함" and rows["더그투스"]["basis"] == "대상 회사 기사에서만 언급"
    assert rows["더그투스"]["n_sources"] == 2                       # 도메인은 2곳이지만 둘 다 대상 회사 기사
    assert (rows["로보팜"]["verified"], rows["로보팜"]["n_sources"]) == ("근거 약함", 1)
    assert k["confirmed_competitors"] == 2

    # 검색: 계획 2 + '경쟁사' 1 + 실재 확인 3 (같은 분야 후보는 검색하지 않는다)
    verify = ['"Dogtooth" fruit harvesting robot', '"Tevel" fruit harvesting robot', '"로보팜" 과채류 수확 로봇']
    assert web.calls == ["국내 수확 로봇 스타트업", "fruit harvesting robot startup", "테스트로봇 경쟁사", *verify]
    assert k["rival_check"]["queries"] == verify and k["rival_check"]["peers_used"] == ["트랙봇"]
    # 판정 근거 풀(pool_ids)에는 같은 분야 후보·실재 확인 검색 근거를 넣지 않는다
    assert ids["news1"] not in k["pool_ids"] and not set(k["rival_check"]["evidence_ids"]) & set(k["pool_ids"])
    assert set(rows["Tevel"]["source_ids"]) <= set(out["registry"])
    # 판정에 확인된 경쟁사 수가 넘어간다 (C2 min_competitors)
    assert [j["dim"] for j in judged] == ["competition"] and judged[0]["company"]["confirmed_competitors"] == 2
    assert "[확인된 경쟁사] 2곳 (국내 1·해외 1)" in judged[0]["analysis"]
    assert "실재 확인 3건" in out["log"][0] and "확인 2곳" in out["log"][0]


def test_verify_search_budget(monkeypatch, judged, set_cfg):
    set_cfg("competition.verify_max_searches", 1)
    out, web, _, _ = _run(monkeypatch, judged)
    k = out["competition"]
    assert web.calls[3:] == ['"Dogtooth" fruit harvesting robot']      # 첫 번째 미확인 경쟁사 1곳만 검색
    rows = {r["name"]: r for r in k["competitors"]}
    assert rows["Tevel"]["verified"] == "근거 약함" and rows["Tevel"]["basis"] == "출처 도메인 1곳"
    assert k["confirmed_competitors"] == 1 and judged[0]["company"]["confirmed_competitors"] == 1


# ── core.judge: C2 min_competitors
def test_rubric_c2_flag():
    qs = {q["id"]: q for d in load_rubric()["dimensions"] for q in d["questions"]}
    assert qs["C2"]["min_competitors"] == 2
    assert [q for q, v in qs.items() if v.get("min_competitors")] == ["C2"]


def test_judge_company_carries_confirmed_count():
    state = {"current": {"official_name": "팜랩"}, "tech": {}, "competition": {"confirmed_competitors": 3}}
    assert judge_company(state)["confirmed_competitors"] == 3
    assert "confirmed_competitors" not in judge_company({**state, "competition": {}})


C2_QUOTE = "팜랩의 수확 로봇은 경쟁사 그린봇보다 딸기 수확 속도가 20% 빠르다는 실증 결과가 나왔다"


@pytest.mark.parametrize("count, answer", [(None, "UNKNOWN"), (1, "UNKNOWN"), (2, "YES"), (3, "YES")])
def test_c2_yes_needs_two_confirmed_competitors(monkeypatch, count, answer):
    reg = SourceRegistry({"W1": {"id": "W1", "kind": "web", "url": "https://news.example.com/1", "site": "예시신문",
                                 "date": "2026-05-01", "title": "팜랩 수확 로봇 비교", "snippet": "", "body": C2_QUOTE + "."}})
    replies = [[{"qid": "C1", "verdict": "UNKNOWN", "quote": "", "evidence_ids": [], "rationale": "근거 없음"},
                {"qid": "C2", "verdict": "YES", "quote": C2_QUOTE, "evidence_ids": ["W1"], "rationale": "실증 비교가 있다"},
                {"qid": "C3", "verdict": "UNKNOWN", "quote": "", "evidence_ids": [], "rationale": "근거 없음"},
                {"qid": "C4", "verdict": "UNKNOWN", "quote": "", "evidence_ids": [], "rationale": "근거 없음"}]]

    class Fake:
        def invoke(self, prompt):
            return Answers(answers=[Answer(**a) for a in replies.pop(0)])
    monkeypatch.setattr(judge, "bounded", lambda schema, role="generator", kind="default": Fake())
    company = {"name": "팜랩", "official_name": "(주)팜랩", "name_en": "Farm Lab"}
    if count is not None:
        company["confirmed_competitors"] = count
    res = judge_dimension("competition", company, ["W1"], reg, "[후보] (주)팜랩", RUN_DATE)
    row = next(r for r in res["rows"] if r["qid"] == "C2")
    assert row["answer"] == answer
    if answer == "UNKNOWN":
        assert f"확인된 경쟁사 {count or 0}곳 — 2곳 이상과 비교해야 함" in row["rationale"]
        assert [x["qid"] for x in res["rejected_yes"]] == ["C2"]
        assert R._human_reason(row["rationale"]) == f"확인된 경쟁사 {count or 0}곳 — 2곳 이상과 비교해야 함"
    else:
        assert row["evidence_ids"] == ["W1"] and not res["rejected_yes"]


# ── 보고서 경쟁사 표
def _report_target(reg: SourceRegistry) -> tuple[dict, dict]:
    ids = {"robo": _web(reg, "https://www.agnews.kr/r1", "수확 로봇 스타트업 비교", "로보팜은 토마토 수확 로봇을 만든다", "competition"),
           "t1": _web(reg, "https://www.tevel-tech.com/", "Tevel Aerobotics", "Tevel flying robots", "competition"),
           "t2": _web(reg, "https://www.reuters.com/tevel", "Tevel raises", "Tevel raised money", "competition"),
           "tb": _web(reg, "https://news-one.com/a", "트랙봇, 시드 투자 유치", "트랙봇이 투자를 받았다")}
    row = lambda name, **kw: {"name": name, "country": "한국", "offering": f"{name} 제품", "vs_target": "-",  # noqa: E731
                              "evidence_ids": [], **kw}
    comp_ = {"competitors": [
        row("로보팜", verified="근거 약함", n_sources=1, source_ids=[ids["robo"]], peer=False),
        row("Tevel", country="이스라엘", verified="확인", n_sources=2, source_ids=[ids["t1"], ids["t2"]], peer=False),
        row("트랙봇", verified="확인", n_sources=3, source_ids=[ids["tb"]], peer=True)],
        "evidence_ids": [ids["robo"]], "pool_ids": [ids["robo"]]}
    return {"name": "테스트로봇", "profile": {"official_name": "테스트로봇", "name": "테스트로봇"}, "competition": comp_}, ids


def test_report_competitor_table_shows_source_count_and_status():
    reg = SourceRegistry()
    tgt, ids = _report_target(reg)
    rows = R._competitor_rows(tgt, reg, [], c2_yes=False)
    assert [r[0] for r in rows] == ["Tevel", "트랙봇", "로보팜"]           # 확인된 경쟁사가 위
    assert [r[4] for r in rows] == ["2곳 (확인)", "3곳 (확인·적격 후보)", "1곳 (근거 약함)"]
    assert rows[0][5] == f"[{ids['t1']}, {ids['t2']}]"                   # 도메인별 첫 근거를 인용
    blocks = R._competition_blocks(tgt, {"competition": "경쟁 구도 요약"}, reg, False, [])
    table = next(b for b in blocks if b["t"] == "table")
    assert table["head"][4] == "출처 수" and len(table["widths"]) == len(table["head"]) == 6
    assert "근거 약함" in table["caption"]


def test_report_competitor_table_old_state_fallback():
    """v3 이전 State(verified 없음)는 표의 근거에서 회사명이 나온 출처 도메인 수만 다시 센다."""
    reg = SourceRegistry()
    a = _web(reg, "https://www.agnews.kr/r1", "수확 로봇 스타트업 비교", "로보팜은 토마토 수확 로봇을 만든다", "competition")
    b = _web(reg, "https://farm-b.com/x", "로보팜 공장 탐방", "로보팜 공장", "competition")
    c = _web(reg, "https://farm-c.com/y", "그린봇 소식", "그린봇 이야기", "competition")
    tgt = {"name": "테스트로봇", "profile": {"official_name": "테스트로봇"}, "competition": {
        "competitors": [{"name": "그린봇", "country": "한국", "offering": "-", "vs_target": "-"},
                        {"name": "로보팜", "country": "한국", "offering": "-", "vs_target": "-"}],
        "evidence_ids": [], "pool_ids": [a, b, c]}}
    rows = R._competitor_rows(tgt, reg, [], c2_yes=False)
    assert [(r[0], r[4]) for r in rows] == [("로보팜", "2곳 (확인)"), ("그린봇", "1곳 (근거 약함)")]
