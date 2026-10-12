"""🔍 스타트업 탐색 에이전트의 안쪽 서브그래프: 다음 적격 후보 1곳을 정한다.

    (진입 · screen 뒤) ─┬─(대기열 있음)──────────────→ pick ─────→ (평가 대상 1곳)
                        ├─(비었고 발굴 라운드 남음)─→ collect → screen → (진입과 같은 분기로 다시)
                        └─(비었고 라운드 소진)───────→ exhausted → (후보 소진)

- collect = v1 agents.discovery.discovery_node (발굴 8채널·교차 신호), screen = v1 agents.eligibility.eligibility_node
  (관문 G1~G6, 확인 못 하면 탈락). 두 함수는 수정 없이 그대로 불러 발굴·관문 결과와 LLM·검색 캐시 키를 보존한다.
- screen 은 관문 결과만 한 번 더 거른다(G2 최근 투자, core/recency.py): 최근 라운드가 평가 기준일 이후이거나 단계 인용이
  예정·추진 중인 라운드면 완료된 투자가 아니므로 '확인 불가'로 탈락시키고 대기열에서 뺀다. 관문 함수의 입력은 그대로라
  캐시 키가 바뀌지 않는다 (동결 파일 agents/eligibility.py 를 고치지 않고 결과를 받는 곳에서 적용).
- screen 은 그 뒤 국내 후보를 DART(금융감독원 전자공시시스템)로 한 번 더 조회한다(dart_check). 관문 판정·대기열 순서는 바꾸지 않고
  후보 기록에 dart 요약(상장 구분·설립일·감사보고서·매출)을 붙이며, 공시가 있으면 근거 저장소에 'DART' 출처로 등록한다.
  DART 에 없는 회사는 {'status': 'not_found'} 만 붙는다(감점 근거로 쓰지 않음). 쓰임새는 tools/dart.py 머리말.
- 입력 규격(DiscoveryIn)에 누적 키(screened·log·rag_traces·evaluations)가 없어서 서브그래프 안의 누적 키는 빈 목록에서
  시작한다. 그래서 출력(DiscoveryOut)에는 이번 탐색에서 새로 생긴 기록만 담기고, 부모 그래프의 Reducer 가 중복 없이 붙인다.
"""
from __future__ import annotations

import functools
import inspect
from typing import Callable

from langgraph.graph import END, START, StateGraph

from core.config import run_date
from core.recency import future_round
from graph.routes import route_discovery_entry
from graph.state import DiscoveryIn, DiscoveryOut, InvestmentState

_BRANCHES = {"pick": "pick", "collect": "collect", "exhausted": "exhausted"}


def pick_node(state: dict) -> dict:
    """대기열 맨 앞 후보를 꺼내 평가 대상으로 정하고, 이전 후보의 분석 결과를 비운다."""
    queue = list(state.get("queue", []))
    current = queue.pop(0)
    n = state.get("iterations", 0) + 1
    msg = f"[평가 {n}] {current['official_name']} ({current['region']}, {current['stage']}, {current['segment_id']})"
    print(msg)
    return {"current": current, "queue": queue, "iterations": n, "founder": {}, "tech": {}, "market": {},
            "competition": {}, "scorecard": {}, "log": [msg]}


def exhausted_node(state: dict) -> dict:
    """대기열이 비었고 발굴 라운드도 다 썼다. 평가한 후보가 없으면 no_eligible(C안), 있으면 exhausted(B안)."""
    n, rounds = state.get("iterations", 0), state.get("discovery_rounds", 0)
    if n == 0:
        reason, msg = "no_eligible", f"[탐색 종료] 발굴 {rounds}라운드에서 적격 후보 없음 → 보고서"
    else:
        reason, msg = "exhausted", f"[탐색 종료] 적격 후보 {n}곳 평가 후 대기열이 비고 발굴 {rounds}라운드 소진 → 보고서"
    print(msg)
    return {"current": None, "end_reason": reason, "log": [msg]}


def recency_guard(screen_fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
    """관문 결과의 G2(최근 투자)를 평가 기준일로 다시 확인한다. 기준일 이후·예정 라운드로 통과한 후보만 탈락으로 바꾸고
    대기열에서 빼며, 나머지 출력은 손대지 않는다 (해당 후보가 없으면 출력이 관문 함수와 완전히 같다)."""
    @functools.wraps(screen_fn)
    def screen(state: dict) -> dict:
        out = screen_fn(state)
        recs = out.get("screened", [])
        if not any(r.get("eligible") for r in recs):
            return out
        date = state.get("run_date") or run_date()  # 관문 함수의 조회일(tools.sources.today)과 같은 평가 기준일
        why = {i: w for i, r in enumerate(recs) if r.get("eligible") and (w := future_round(r, date))}
        if not why:
            return out
        names = {recs[i]["name"] for i in why}
        msg = f"[적격성 보정] G2 기준일 이후·예정 라운드 {len(names)}곳 탈락: {', '.join(sorted(names))}"
        print(msg)
        fixed = {**out, "log": [*out.get("log", []), msg],
                 "screened": [{**r, "eligible": False, "reason": f"G2 {why[i]}"} if i in why else r
                              for i, r in enumerate(recs)]}
        if "queue" in out:
            old = len(state.get("queue") or [])  # 관문 함수는 기존 대기열 뒤에 이번 통과 후보를 붙인다 → 새로 붙은 것만 거른다
            fixed["queue"] = out["queue"][:old] + [c for c in out["queue"][old:] if c.get("name") not in names]
        return fixed
    return screen


def dart_check(screen_fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
    """관문 결과의 국내 후보(region KR)를 DART 로 조회해 기록·대기열에 dart 요약을 붙인다. 관문 판정(eligible·reason)과
    대기열 순서는 그대로다. 공시가 있는 적격 후보(matched)만 근거 저장소에 등록하고(registry), 국내 후보가 없으면 출력이 그대로다.
    DART 조회 오류는 평가를 멈추지 않는다(unavailable). __wrapped__ 는 가장 안쪽 관문 함수를 가리킨다."""
    @functools.wraps(screen_fn)
    def screen(state: dict) -> dict:
        out = screen_fn(state)
        recs = out.get("screened", [])
        kr = [r for r in recs if r.get("region") == "KR" and r.get("name") and "dart" not in r]
        if not kr:
            return out
        from tools import dart
        from tools.sources import SourceRegistry

        date = state.get("run_date") or run_date()
        reg = SourceRegistry(out.get("registry", state.get("registry")))
        n0 = len(reg.data)
        info = {r["name"]: dart.check(r, reg if r.get("eligible") else None, date) for r in kr}  # 탈락 후보는 등록 안 함
        counts: dict[str, int] = {}
        for v in info.values():
            counts[v["status"]] = counts.get(v["status"], 0) + 1
        hits = [f"{k}({v['market']}, 설립 {v.get('est_dt') or '미상'}, 감사보고서 {v['audit_reports']}건"
                f"{', 매출 ' + str(v['revenue'][0]['year']) + '년' if v.get('revenue') else ''})"
                for k, v in info.items() if v["status"] == "matched"]
        listed = [k for k, v in info.items() if dart.listed_warning(v)]
        msg = (f"[DART] 국내 {len(kr)}곳 조회 → " + ", ".join(f"{s} {n}곳" for s, n in sorted(counts.items()))
               + (f" · 공시 확인: {'; '.join(hits)}" if hits else "")
               + (f" · 상장 의심: {', '.join(listed)}" if listed else ""))
        print(msg)
        fixed = {**out, "log": [*out.get("log", []), msg],
                 "screened": [{**r, "dart": info[r["name"]]} if r.get("name") in info and r in kr else r for r in recs]}
        if "queue" in out:
            old = len(state.get("queue") or [])  # 이번 관문이 붙인 대기열 항목에만 붙인다 (기존 항목은 이전 라운드에 붙음)
            fixed["queue"] = out["queue"][:old] + [{**c, "dart": info[c["name"]]} if c.get("name") in info else c
                                                   for c in out["queue"][old:]]
        if len(reg.data) != n0:
            fixed["registry"] = reg.data
        return fixed
    screen.__wrapped__ = inspect.unwrap(screen_fn)  # 테스트·설계 확인용: 관문 함수(v1)를 그대로 부른다
    return screen


def build_discovery_graph(collect_fn: Callable[[dict], dict] | None = None,
                          screen_fn: Callable[[dict], dict] | None = None):
    """탐색 서브그래프를 컴파일해 돌려준다. 테스트는 collect_fn·screen_fn 에 가짜 함수를 넣는다."""
    if collect_fn is None:
        from agents.discovery import discovery_node as collect_fn
    if screen_fn is None:
        from agents.eligibility import eligibility_node as screen_fn

    g = StateGraph(InvestmentState, input_schema=DiscoveryIn, output_schema=DiscoveryOut)
    g.add_node("collect", collect_fn)      # 발굴: 채널 8개 검색 → 후보 추출 (v1 그대로)
    # 적격성 관문 G1~G6 (v1 그대로) + G2 기준일 이후·예정 라운드 탈락 + 국내 후보 DART 공시 조회(판정은 그대로)
    g.add_node("screen", dart_check(recency_guard(screen_fn)))
    g.add_node("pick", pick_node)          # 다음 후보 1곳 선택, 후보별 분석 키 초기화
    g.add_node("exhausted", exhausted_node)  # 후보 소진: current 없음, 종료 사유 기록

    g.add_conditional_edges(START, route_discovery_entry, _BRANCHES)
    g.add_edge("collect", "screen")
    g.add_conditional_edges("screen", route_discovery_entry, _BRANCHES)
    g.add_edge("pick", END)
    g.add_edge("exhausted", END)
    return g.compile()
