"""메인 그래프: 노션 Graph(안)을 모양 그대로 따르고 👤 창업자 평가 한 노드만 더했다.

    Graph(안)               이 코드의 노드
    A 스타트업 탐색     →   discover     (🔍 서브그래프 collect → screen → pick / exhausted, graph/discovery_graph.py)
    (추가) 창업자 평가  →   founder
    B 기술 요약         →   tech
    C 시장성 평가       →   market
    D 경쟁사 비교       →   competition
    E 투자 판단         →   decide
    (추가) 심사역 검토  →   review       (👤 사람 승인 게이트 — workflow.human_review 일 때만 멈춤, 기본은 그냥 통과)
    F 보고서 생성       →   report

    START → discover ─(평가 대상 있음)→ founder → tech → market → competition → decide
               │                                                                 ├─(투자·보류, 평가 상한 전)→ discover
               └─(후보 소진)→ review → report → END                               └─(평가 상한 도달)→ review → report → END

- 간선 집합은 설계서의 DESIGN_EDGES(계약 C2)와 같다. 병렬 간선은 없다(경쟁사가 tech.claims 를, 투자 판단이 market 을 받아 쓰는 순서 의존).
- 첫 투자 추천에서 멈추지 않는다(workflow.stop_on_invest=false). 먼저 평가한 후보가 결론이 되지 않도록 상한까지 평가하고,
  보고서가 투자 기준 통과 후보 중 배수 1위를 대상으로 삼는다. true 로 두면 Graph(안)의 '투자 추천 → 보고서 생성' 그대로다.
- 분기 규칙은 graph/routes.py 의 순수 함수다. 보정 실행도 report 노드에서 끝난다(report 가 보정 모드에서는 렌더링하지 않음).
- 에이전트 모듈은 함수 안에서 import 한다. 그래서 nodes 로 가짜 노드를 넣은 테스트는 에이전트 모듈(검색·LLM 의존성)을 읽지 않는다.
"""
from __future__ import annotations

import importlib
import os

from langgraph.graph import END, START, StateGraph

from graph.routes import route_after_decide, route_after_discover
from graph.state import InvestmentState

NODE_NAMES = ("discover", "founder", "tech", "market", "competition", "decide", "review", "report")

# 노드 이름 → (모듈, 함수). discover 는 서브그래프라 build_discovery_graph() 로 만든다
_AGENT_NODES = {
    "founder": ("agents.founder", "founder_node"),              # 👤 창업자 평가 (창업자 30%)
    "tech": ("agents.tech", "tech_node"),                       # 🗜️ 기술 요약 (제품/기술력 15%)
    "market": ("agents.market", "market_node"),                 # 📊 시장성 평가 (시장성 25%, Agentic RAG)
    "competition": ("agents.competition", "competition_node"),  # 🥊 경쟁사 비교 (경쟁 우위 10%)
    "decide": ("agents.decision", "decision_node"),             # 🧮 투자 판단 (실적·투자조건 20% + 동종 대비 배수)
    "review": ("graph.review", "review_node"),                  # 👤 심사역 검토 (사람 승인 게이트, 기본은 그냥 통과)
    "report": ("agents.report", "report_node"),                 # 📝 보고서 생성
}


# 실패해도 실행을 멈추지 않는 분석 노드: 실패하면 자기 칸에 {"error": …} 만 남기고, 투자 판단 노드가 그 기준을
# 근거 묶음으로 대신 판정한다(agents/decision.py — criterion 이 없으면 여기서 판정). 실패가 있는 후보는 투자 추천하지 않는다.
GUARDED = ("founder", "tech", "market", "competition")


def guard(name: str, fn):
    """분석 노드 실패를 State 의 errors 칸에 기록하고 계속 간다 (액터별 오류 처리 — 검색·LLM 공급자 오류가 한 후보 때문에
    10곳 평가 전체를 멈추지 않게). 재현 실행(--offline)은 결과가 제출본과 같아야 하므로 실패를 그대로 올린다."""
    def run(state: dict) -> dict:
        try:
            return fn(state)
        except Exception as e:
            from core.cost import BudgetExceeded

            if os.getenv("REPLAY_OFFLINE") or isinstance(e, BudgetExceeded):   # 재현 실행·비용 상한 초과는 실행 전체를 멈춘다
                raise
            cur = state.get("current") or {}
            cand = cur.get("official_name") or cur.get("name")
            err = {"node": name, "candidate": cand, "type": type(e).__name__, "message": str(e)[:300]}
            msg = f"[{name}] {cand}: 실패({type(e).__name__}) → 투자 판단 노드가 이 기준을 대신 판정, 이 후보는 투자 추천 제외"
            print(msg)
            return {name: {"error": f"{err['type']}: {err['message']}"}, "errors": [err], "log": [msg]}
    run.__name__ = getattr(fn, "__name__", name)
    return run


def _with_status(fn):
    """보고서 노드가 끝날 때 실행 상태를 적는다: 실패 기록이 없으면 SUCCESS, 있으면 PARTIAL."""
    def run(state: dict) -> dict:
        return {**fn(state), "status": "PARTIAL" if state.get("errors") else "SUCCESS"}
    run.__name__ = getattr(fn, "__name__", "report")
    return run


def _default_node(name: str):
    if name == "discover":  # 🔍 스타트업 탐색 (서브그래프)
        from graph.discovery_graph import build_discovery_graph

        return build_discovery_graph()
    module, fn = _AGENT_NODES[name]
    return getattr(importlib.import_module(module), fn)


def build_graph(nodes: dict | None = None, checkpointer=None):
    """메인 그래프를 컴파일한다. nodes={'decide': 가짜 함수, …} 로 일부 노드를 바꿔 끼울 수 있다(테스트용).
    checkpointer: 사람 승인(--review)처럼 멈췄다 이어서 실행할 때만 준다 (멈춘 지점의 State 를 저장)."""
    nodes = dict(nodes or {})
    unknown = set(nodes) - set(NODE_NAMES)
    if unknown:
        raise ValueError(f"알 수 없는 노드 이름: {sorted(unknown)} (가능: {NODE_NAMES})")

    g = StateGraph(InvestmentState)
    for name in NODE_NAMES:
        fn = nodes[name] if name in nodes else _default_node(name)
        g.add_node(name, guard(name, fn) if name in GUARDED else _with_status(fn) if name == "report" else fn)

    g.add_edge(START, "discover")
    g.add_conditional_edges("discover", route_after_discover, {"founder": "founder", "report": "review"})
    g.add_edge("founder", "tech")
    g.add_edge("tech", "market")
    g.add_edge("market", "competition")
    g.add_edge("competition", "decide")
    g.add_conditional_edges("decide", route_after_decide, {"report": "review", "discover": "discover"})
    g.add_edge("review", "report")   # 👤 보고서 직전 사람 승인 (workflow.human_review 일 때만 멈춤)
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
