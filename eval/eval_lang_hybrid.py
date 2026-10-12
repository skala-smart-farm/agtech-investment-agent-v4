"""언어 인식 하이브리드(rag/hybrid_lang.py) 측정: 지금 런타임 하이브리드 · Dense 만 · 언어 인식(filter / impute)을
같은 70문항(질문은 모두 한국어, ko/en 은 정답 문서 언어)과 같은 k(candidate_k)로 잰다. LLM 호출 없음.

판정 규칙(측정 전에 고정, eval_final_retriever 와 같은 규칙): 1) 70문항 전체 Hit@4 최대 2) 같으면 MRR@4 최대
3) 완전 동률이면 지금 런타임 설정. 결과는 outputs/eval/lang_hybrid.json · .md

    python -m eval.eval_lang_hybrid
"""
from __future__ import annotations

import json

from core.config import get_config, path
from eval.eval_final_retriever import ALT_EMB, _score
from eval.eval_retrieval import SETS, _load
from rag.hybrid_lang import LanguageAwareHybrid
from rag.index import get_bm25, get_hybrid_retriever, get_vectorstore

GRID = (0.2, 0.3, 0.4, 0.5)


def main() -> None:
    cfg = get_config()
    k = cfg.rag.candidate_k
    w = [float(x) for x in cfg.rag.ensemble_weights]
    qs = {s: _load(f) for s, f in SETS.items() if path(f).exists()}
    allq = [q for v in qs.values() for q in v if not q.get("added")]
    groups = {"all": allq, **{lang: [q for q in allq if q["lang"] == lang] for lang in ("ko", "en")}}
    bm25 = get_bm25(k)
    dense = get_vectorstore(*ALT_EMB).as_retriever(search_kwargs={"k": k})

    specs = [("runtime hybrid (EnsembleRetriever)", get_hybrid_retriever(), w),
             ("dense only", dense, [0.0, 1.0])]
    for mode in ("filter", "impute"):
        for b in GRID:
            gw = [b, round(1 - b, 1)]
            specs.append((f"lang-{mode} {gw[0]}:{gw[1]}", LanguageAwareHybrid(bm25=bm25, dense=dense, weights=gw, mode=mode, k=k), gw))
    rows = [{"name": n, "weights[bm25,dense]": ws, "k": k, **_score(r, groups, k)} for n, r, ws in specs]
    best = max(enumerate(rows), key=lambda t: (t[1]["all"]["Hit@4"], t[1]["all"]["MRR@4"], t[0] == 0))[1]
    out = {"rule": "1) 70문항 Hit@4 최대 2) 같으면 MRR@4 최대 3) 완전 동률이면 지금 런타임", "pick": best["name"], "rows": rows}
    path("outputs/eval/lang_hybrid.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["| 설정 | 전체 Hit@4 | 전체 MRR@4 | 한국어 문서 Hit@4 · MRR@4 | 영어 문서 Hit@4 · MRR@4 |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['name']}{' **(선택)**' if r is best else ''} | {r['all']['Hit@4']:.3f} | {r['all']['MRR@4']:.3f} | "
                     f"{r['ko']['Hit@4']:.3f} · {r['ko']['MRR@4']:.3f} | {r['en']['Hit@4']:.3f} · {r['en']['MRR@4']:.3f} |")
    md = ("# 언어 인식 하이브리드 측정\n\n질문 70개(모두 한국어), k=" + str(k) + ", 판정 규칙: " + out["rule"] + "\n\n"
          + "\n".join(lines) + "\n")
    path("outputs/eval/lang_hybrid.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
