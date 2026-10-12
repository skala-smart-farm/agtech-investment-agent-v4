"""👤 사람 승인 게이트 (Human-in-the-loop): 보고서를 쓰기 직전에 심사역이 투자 추천 대상을 확인한다.

교재 7장 4-1-3 HumanInTheLoopMiddleware 와 같은 구조 — 흐름을 멈추고(interrupt) 사람이 승인·수정·거부한 뒤 재개한다.
멈춘 지점의 State 는 체크포인터에 저장되므로 같은 thread_id 로 이어서 실행한다(app.py --review).

- 기본은 끔(workflow.human_review: false): 배치 실행·키 없는 재현(--offline)은 이 노드를 그냥 지나간다
- 투자 기준 통과 후보가 없으면 승인할 대상이 없으므로 멈추지 않는다(모두 보류 보고서)
- 답(resume 값)은 코드가 검증한다: approve / pick:<투자 기준 통과 후보 이름> / reject:<사유>. 형식이 틀리거나
  기준 통과 후보가 아닌 이름이면 승인으로 보지 않고 다시 묻지도 않는다 — 'invalid' 로 기록하고 원래 제안대로 간다
"""
from __future__ import annotations

from langgraph.types import interrupt

from core.config import get_config

ACTIONS = ("approve", "pick", "reject")


def proposal(evals: list[dict]) -> dict | None:
    """심사역에게 보여 줄 제안: 투자 기준 통과 후보(배수 순)와 보고서 대상. 통과 후보가 없으면 None."""
    from agents.decision import eval_rank_key

    passed = sorted((e for e in evals if e.get("decision") == "투자"), key=eval_rank_key)
    if not passed:
        return None
    return {"target": passed[0]["name"],
            "passed": [{"name": e["name"], "score100": e.get("score100")} for e in passed],
            "answers": "approve | pick:<후보 이름> | reject:<사유>"}


def parse(answer, prop: dict) -> dict:
    """resume 값 → {action, target, reason}. 형식이 틀리면 action 'invalid'(원래 제안대로 진행)."""
    text = str(answer.get("answer") if isinstance(answer, dict) else answer or "").strip()
    action, _, arg = text.partition(":")
    action, arg = action.strip().lower(), arg.strip()
    names = [p["name"] for p in prop["passed"]]
    if action == "approve":
        return {"action": "approve", "target": prop["target"], "reason": arg or None}
    if action == "pick" and arg in names:
        return {"action": "pick", "target": arg, "reason": None}
    if action == "reject" and arg:
        return {"action": "reject", "target": prop["target"], "reason": arg}
    return {"action": "invalid", "target": prop["target"], "reason": f"알 수 없는 답 {text!r} → 제안대로 진행"}


def review_node(state: dict) -> dict:
    cfg = get_config()
    if not cfg.workflow.get("human_review") or cfg.workflow.get("calibrate"):
        return {}
    prop = proposal(state.get("evaluations") or [])
    if prop is None:
        return {"log": ["[심사역 검토] 투자 기준 통과 후보 없음 → 승인 없이 모두 보류 보고서"]}
    decision = parse(interrupt(prop), prop)
    msg = f"[심사역 검토] {decision['action']} — 대상 {decision['target']}" + (f" ({decision['reason']})" if decision["reason"] else "")
    print(msg)
    return {"review": decision, "log": [msg]}
