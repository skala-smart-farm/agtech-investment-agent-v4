"""반복 실행 안정성(PassK): 같은 입력으로 K번 새로 실행해 보고서 대상·후보별 결정이 지금 결과(재현 실행)와 같은지 잰다.

왜: 같은 입력이면 같은 품질이어야 한다(2026-10-12 수업 — 성공 기준의 Stability, 교재 부록 A PassK).
실측 계기: 시장 분석 프롬프트 한 줄을 바꾸자 1위가 바뀌었다 → 모델 출력의 편차가 순위를 바꿀 수 있는지 직접 잰다.

- 새로 부르는 것은 LLM 뿐이다(--no-llm-cache). 검색·공시·본문 캐시는 같은 것을 쓰므로, 차이는 모델 출력 편차에서 온다.
  (모델 출력이 달라져 새 검색어가 생기면 그 검색만 실시간으로 한다)
- 실행마다 replay/ 의 사본(LLM 캐시 제외)을 따로 써서 제출본 캐시를 건드리지 않는다. 결과는 outputs/eval/passk/run{i}/
- 판정: K번 모두 보고서 대상이 지금 대상과 같으면 통과(성공 기준 '안정성'). 후보별 결정 일치율·배수 차이도 남긴다.

    python -m eval.eval_passk --k 3        # 유료: LLM 을 K번 새로 부른다 (1회 약 $0.6, workflow.max_llm_usd 상한)
    python -m eval.eval_passk --summarize  # 이미 돌린 결과만 다시 집계 (무료)
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.config import ROOT, get_config, path

OUT = "outputs/eval/passk"


def _run(i: int, work: Path) -> int:
    cache = work / f"cache_{i}"
    shutil.copytree(ROOT / get_config().cache.dir, cache, ignore=shutil.ignore_patterns("llm_cache.sqlite"))
    log = path(f"{OUT}/run{i}.log")
    with open(log, "w", encoding="utf-8") as f:
        return subprocess.run([sys.executable, "-W", "ignore", "app.py", "--no-llm-cache", "--cache-dir", str(cache),
                               "--out", f"{OUT}/run{i}"], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT).returncode


def _view(run: dict) -> dict:
    return {"target": (run.get("report") or {}).get("target"), "mode": (run.get("report") or {}).get("mode"),
            "decisions": {e["name"]: {"decision": e.get("decision"), "score100": e.get("score100")}
                          for e in run.get("evaluations") or []},
            "usd": (run.get("llm_cost") or {}).get("usd")}


def summarize() -> dict:
    base = _view(json.loads(path("outputs/run_log.json").read_text(encoding="utf-8")))
    runs = [_view(json.loads(p.read_text(encoding="utf-8"))) for p in sorted(path(OUT).glob("run*/run_log.json"))]
    k = len(runs)
    same_target = sum(r["target"] == base["target"] for r in runs)
    rows = []
    for name, b in base["decisions"].items():
        got = [r["decisions"].get(name) for r in runs]
        scores = [g["score100"] for g in got if g and g.get("score100") is not None]
        rows.append({"name": name, "base": b, "same_decision": sum(bool(g) and g["decision"] == b["decision"] for g in got),
                     "evaluated": sum(bool(g) for g in got),
                     "score_range": [min(scores + [b["score100"]]), max(scores + [b["score100"]])] if scores else None})
    out = {"k": k, "base_target": base["target"], "targets": [r["target"] for r in runs], "same_target": same_target,
           "pass": k > 0 and same_target == k, "rows": rows, "usd": [r["usd"] for r in runs],
           "candidate_sets_same": all(set(r["decisions"]) == set(base["decisions"]) for r in runs)}
    path("outputs/eval/passk.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 반복 실행 안정성 (PassK, K={k})", "",
             f"지금 대상 {base['target']} · 새 실행 대상 {', '.join(str(t) for t in out['targets'])} → "
             f"대상 일치 {same_target}/{k} ({'통과' if out['pass'] else '미달'}) · 평가 후보 구성 "
             f"{'같음' if out['candidate_sets_same'] else '다름'}", "",
             "| 후보 | 지금 결정 (배수) | 같은 결정 | 배수 범위 (지금 포함) |", "|---|---|---|---|"]
    for r in rows:
        rng = f"{r['score_range'][0]} ~ {r['score_range'][1]}" if r["score_range"] else "-"
        lines.append(f"| {r['name']} | {r['base']['decision']} ({r['base']['score100']}) | {r['same_decision']}/{k} | {rng} |")
    md = "\n".join(lines) + "\n"
    path("outputs/eval/passk.md").write_text(md, encoding="utf-8")
    print(md)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--summarize", action="store_true")
    a = p.parse_args()
    if not a.summarize:
        with tempfile.TemporaryDirectory(prefix="passk_") as work, ThreadPoolExecutor(max_workers=a.k) as ex:
            codes = list(ex.map(lambda i: _run(i, Path(work)), range(1, a.k + 1)))
        print(f"실행 종료 코드: {codes}")
    summarize()


if __name__ == "__main__":
    main()
