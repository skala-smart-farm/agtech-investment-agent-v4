"""성공 기준 측정: 실행 기록(run_log.json)을 docs/goal.md 의 목표치(config success_criteria)와 비교한다. LLM 호출 없음.

왜: 성공 기준은 '좋은 보고서' 같은 정성 표현이 아니라 판정 가능한 숫자여야 한다 (2026-10-12 수업 — 출처 명시율 100%,
Faithfulness ≥ 95%, 상충 0건, 호출·비용 상한 등). 본 실행(투자 보고서)과 보류 시나리오를 함께 잰다.
결과: outputs/eval/success_criteria.json · .md (README 의 '성공 기준' 표가 읽는다)

    python -m eval.success_criteria
"""
from __future__ import annotations

import json

from core.config import get_config, path

RUNS = {"투자 보고서": "outputs/run_log.json", "보류 시나리오": "outputs/scenario_hold/run_log.json"}


def _unsupported_yes(run: dict) -> int:
    """인용문이나 근거 id 가 없는 YES 판정 수 (근거가 없으면 '미확인'이어야 한다)."""
    return sum(1 for e in run.get("evaluations") or [] for r in e.get("rows") or []
               if r.get("answer") == "YES" and not (r.get("quote") and r.get("evidence_ids")))


def measure(run: dict, cfg) -> dict:
    c = (run.get("report") or {}).get("checks") or {}
    na = c.get("number_audit") or {}
    contradictions = (len(na.get("conflicts") or []) + len(na.get("cagr") or [])
                      + len(c.get("scorecard_consistency_violations") or []) + len(c.get("team_table_conflicts") or [])
                      + (0 if c.get("conclusion_matches_scorecard", True) else 1))
    return {
        "citation_rate": (c.get("citation_coverage") or {}).get("rate"),
        "faithfulness": (c.get("faithfulness") or {}).get("rate"),
        "complete": all(c.get(k) for k in ("required_items_ok", "chapter_refs_ok", "refs_match_citations")),
        "contradictions": contradictions,
        "format": all(c.get(k) for k in ("pages_ok", "summary_ok", "reference_format_ok")),
        "portal_unresolved": (c.get("portal_refs") or {}).get("unresolved"),
        "unsupported_yes": _unsupported_yes(run),
        "node_errors": len(run.get("errors") or []),
        "run_cost_usd": float(cfg.report.get("fresh_cost_usd") or cfg.report.get("run_cost_usd", 0)),
    }


# (키, 기준 이름, 측정 방법, 목표 표기, 통과 여부 함수)
CRITERIA = [
    ("citation_rate", "근거성 — 출처 명시율", "숫자가 든 사실 문장 중 인용이 붙은 비율", lambda t: f"{t:.0%}", lambda v, t: v is not None and v >= t),
    ("faithfulness", "충실성 — 근거가 문장을 뒷받침", "인용 문장마다 LLM 판정(core/faithfulness.py)", lambda t: f"≥ {t:.0%}",
     lambda v, t: v is not None and v >= t),
    ("complete", "완결성 — 필수 항목", "SUMMARY 5칸·목차·장 번호 참조·REFERENCE 와 인용 일치", lambda t: "100%", lambda v, t: v is True),
    ("contradictions", "일관성 — 상충 진술", "같은 연도 다른 값·성장률 불일치·평가표와 다른 결론·팀 표 충돌", lambda t: f"{t}건",
     lambda v, t: v is not None and v <= t),
    ("format", "형식 — 분량·REFERENCE", "5쪽 이내·SUMMARY A4 절반 이내·REFERENCE 3유형 형식(날짜 포함)", lambda t: "통과",
     lambda v, t: v is True),
    ("portal_unresolved", "원 출처 — 포털 전재본", "원 매체 기사를 찾지 못한 포털 주소 인용", lambda t: f"{t}건",
     lambda v, t: v is not None and v <= t),
    ("unsupported_yes", "오류 처리 — 근거 없는 YES", "인용문·근거 id 없는 YES 판정", lambda t: f"{t}건", lambda v, t: v <= t),
    ("node_errors", "안정 운영 — 노드 실패", "분석 노드 실패 기록(State.errors)", lambda t: f"{t}건", lambda v, t: v <= t),
    ("run_cost_usd", "비용 — 새로 평가 1회", "심층 평가 10곳, gpt-4.1-mini, LLM 캐시 없이 실측 최댓값(config report.fresh_cost_usd)", lambda t: f"≤ ${t:.2f}",
     lambda v, t: v <= t),
]


def _fmt(k: str, v) -> str:
    if v is None:
        return "-"
    if k == "stability":
        return f"{v} 대상 일치"
    if isinstance(v, bool):
        return "통과" if v else "미달"
    if k in ("citation_rate", "faithfulness"):
        return f"{v:.0%}"
    if k == "run_cost_usd":
        return f"${v:.2f}"
    return f"{v}건"


def main() -> dict:
    cfg = get_config()
    targets = dict(cfg.success_criteria)
    runs = {name: json.loads(path(f).read_text(encoding="utf-8")) for name, f in RUNS.items() if path(f).exists()}
    measured = {name: measure(r, cfg) for name, r in runs.items()}
    rows = []
    for k, label, how, show, ok in CRITERIA:
        t = targets.get(k)
        vals = {name: m[k] for name, m in measured.items()}
        rows.append({"key": k, "criterion": label, "how": how, "target": show(t), "values": vals,
                     "pass": all(ok(v, t) for v in vals.values())})
    pk = json.loads(path("outputs/eval/passk.json").read_text(encoding="utf-8")) if path("outputs/eval/passk.json").exists() else None
    rows.append({"key": "stability", "criterion": "안정성 — 반복 실행", "how": "LLM 캐시 없이 K회 새로 실행 시 보고서 대상 일치(PassK)",
                 "target": "K=3 일치", "pass": pk["pass"] if pk and pk["k"] else None,
                 "values": {name: (f"{pk['same_target']}/{pk['k']}" if pk and pk["k"] and name == "투자 보고서" else None)
                            for name in measured},
                 "note": None if pk and pk["k"] else "미측정 — python -m eval.eval_passk --k 3 (유료)"})
    out = {"targets": targets, "rows": rows,
           "passed": sum(r["pass"] is True for r in rows), "failed": sum(r["pass"] is False for r in rows),
           "unmeasured": sum(r["pass"] is None for r in rows)}
    path("outputs/eval/success_criteria.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    names = list(measured)
    lines = ["| 기준 | 측정 방법 | 목표 | " + " | ".join(names) + " | 판정 |", "|---" * (4 + len(names)) + "|"]
    for r in rows:
        verdict = {True: "✅", False: "❌", None: "미측정"}[r["pass"]]
        lines.append(f"| {r['criterion']} | {r['how']} | {r['target']} | "
                     + " | ".join(_fmt(r["key"], r["values"][n]) for n in names) + f" | {verdict} |")
    md = (f"# 성공 기준 측정\n\n목표는 docs/goal.md · config.yaml success_criteria. 통과 {out['passed']} · 미달 {out['failed']}"
          f" · 미측정 {out['unmeasured']}\n\n" + "\n".join(lines) + "\n")
    path("outputs/eval/success_criteria.md").write_text(md, encoding="utf-8")
    print(md)
    return out


if __name__ == "__main__":
    main()
