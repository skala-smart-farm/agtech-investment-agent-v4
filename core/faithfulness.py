"""보고서 문장 충실성 검사: 문장에 붙인 인용 근거가 그 문장을 실제로 뒷받침하는가.

왜: 각주가 달렸다고 근거가 되는 것은 아니다 (2026-10-12 수업 피드백 — 서로 반대되는 두 문장에 같은 레퍼런스가 달렸는데
레퍼런스는 한쪽만 지지한 사례). 숫자는 agents/report.py 의 숫자 대조가 코드로 보고, 여기서는 문장 전체의 사실 주장을 본다.

- 판정(확률)은 LLM(models.judge), 통과율·다시 쓰기 채택(결정론)은 코드가 정한다 (교재 부록 B: Judge 와 Gate 분리)
- 단위: 인용([W..]/[D..])이 붙은 문장 하나. 근거 원문에서 문장과 낱말이 가장 많이 겹치는 구간만 보여 준다(문서 전체 X)
- 결과는 checks['faithfulness'] = {n, supported, rate, target, unsupported[...]} — 성공 기준(충실성 ≥ target)과 비교
"""
from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel, Field

from core.llm_bounded import bounded
from core.prompts import render

WINDOW, STEP, MAX_CONTEXT = 1200, 400, 6000   # 근거 1개면 구간 2개, 여러 개면 근거마다 1개(최대 5개)
_WORDS = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}|\d[\d,.]*")


class Support(BaseModel):
    supported: bool = Field(description="인용 근거가 문장의 사실 주장을 모두 뒷받침하면 true, 하나라도 근거에 없거나 다르면 false")
    reason: str = Field(description="판정 이유 한 문장 (false 면 근거에 없는 부분을 짚는다)")


def best_windows(sentence: str, text: str, n: int = 2) -> list[str]:
    """근거 원문에서 문장과 낱말이 가장 많이 겹치는 구간 n개 (원문 순서대로). 짧은 원문은 그대로."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= WINDOW:
        return [text] if text else []
    words = {w.lower() for w in _WORDS.findall(sentence)}
    scored = []
    for i in range(0, max(1, len(text) - WINDOW + STEP), STEP):
        chunk = text[i:i + WINDOW]
        scored.append((sum(1 for w in words if w in chunk.lower()), i))
    picked: list[int] = []
    for _, i in sorted(scored, key=lambda t: (-t[0], t[1])):
        if all(abs(i - j) >= WINDOW for j in picked):
            picked.append(i)
        if len(picked) == n:
            break
    return [text[i:i + WINDOW] for i in sorted(picked)]


def judge_sentence(sentence: str, ids: list[str], text_of: Callable[[str], str],
                   source_of: Callable[[str], str]) -> Support:
    parts, used = [], 0
    per_id = 2 if len(ids) == 1 else 1   # 여러 근거를 함께 볼 때 앞 근거가 자리를 다 차지하지 않게
    for i in ids:
        for w in best_windows(sentence, text_of(i), n=per_id):
            if used + len(w) > MAX_CONTEXT:
                break
            parts.append(f"[{i}] ({source_of(i)}) {w}")
            used += len(w)
    if not parts:
        return Support(supported=False, reason="인용 근거의 원문이 없다")
    return bounded(Support, "judge").invoke(render("faithfulness", sentence=sentence, evidence="\n\n".join(parts)))


def check(units: list[tuple[str, str, list[str]]], text_of: Callable[[str], str], source_of: Callable[[str], str],
          target: float, by_code: Callable[[str, str], Support | None] | None = None) -> dict:
    """units: (어디, 문장, 인용 근거 id). 같은 문장·인용은 한 번만 판정한다.
    by_code(어디, 문장) 가 판정을 돌려주면 LLM 대신 그것을 쓴다 (평가표에서 나온 문장처럼 코드로 대조할 수 있는 것)."""
    seen: dict[tuple, Support] = {}
    rows = []
    for where, sent, ids in units:
        k = (sent, tuple(ids))
        if k not in seen:
            seen[k] = (by_code and by_code(where, sent)) or judge_sentence(sent, ids, text_of, source_of)
        rows.append((where, sent, seen[k]))
    ok = sum(r[2].supported for r in rows)
    failed = [{"where": w, "sentence": s, "reason": v.reason} for w, s, v in rows if not v.supported]
    return {"n": len(rows), "supported": ok, "rate": round(ok / len(rows), 3) if rows else None, "target": target,
            "unsupported": [f"[{f['where']}] {f['sentence'][:120]} — {f['reason']}" for f in failed], "failed": failed}


def _overlap(sentence: str, text: str) -> int:
    t = (text or "").lower()
    return sum(1 for w in {w.lower() for w in _WORDS.findall(sentence)} if w in t)


def recite(sentence: str, pool: list[str], text_of: Callable[[str], str], source_of: Callable[[str], str],
           k: int = 4) -> list[str] | None:
    """근거가 문장을 뒷받침하지 않을 때 문장은 그대로 두고 인용만 바로잡는다: 보고서 입력에 있던 근거 중 문장과 낱말이
    가장 많이 겹치는 k개를 함께 보여 주고 다시 판정해, 뒷받침하면 그 id 들을 돌려준다. 못 찾으면 None (사실이 근거에 없음)."""
    ranked = sorted(pool, key=lambda i: -_overlap(sentence, text_of(i)))[:k]
    if not ranked:
        return None
    return ranked if judge_sentence(sentence, ranked, text_of, source_of).supported else None
