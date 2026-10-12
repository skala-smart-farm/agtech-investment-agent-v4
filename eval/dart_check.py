"""DART 연동 1단계 확인: 이번 실행의 적격 후보·평가 후보 중 국내 회사를 DART 에서 찾아 쓸모를 표로 남긴다.

    python -m eval.dart_check                         # outputs/run_log.json 기준, 캐시({cache.dir}/dart/)를 쓰고 없으면 조회
    python -m eval.dart_check --env /경로/.env          # .env 가 저장소 밖(작업 사본 등)에 있을 때. 키 값은 출력하지 않는다
    REPLAY_OFFLINE=1 python -m eval.dart_check        # 캐시만으로 다시 만든다 (키 불필요)

표: 회사 · 구분(적격·평가·관문 탈락) · 찾음 여부 · 상장 구분(corp_cls) · 설립일(est_dt) · 감사보고서 유무(최근 5년, 공시 유형 F) ·
매출 수치 유무(fnlttSinglAcnt 주요계정 / 감사보고서 원문 요약).
- 회사 찾기: corpCode.xml 회사명을 (주)·주식회사·농업회사법인 등을 지우고 정확히 같은 것만 (없을 때만 기호·공백 무시)
- 국내 법인만 대상이다(해외 후보는 '대상 아님'). 관문에서 탈락한 국내 후보도 참고 행으로 조회한다(대조군)
결과: outputs/eval/dart_check.json — 표 행 + 찾은 회사의 DART 원본 응답(company.json · list.json · fnlttSinglAcnt)과 감사보고서 요약값.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date

from core.config import ROOT, get_config, path
from tools import dart

OUT = "outputs/eval/dart_check.json"


def _targets(run_log: dict) -> list[dict]:
    """run_log 의 screened(관문 기록)·evaluations(평가) → 회사별 행 재료. 평가 이름(공식명)도 표기로 함께 쓴다."""
    evaluated = {e["name"]: e for e in run_log.get("evaluations", [])}
    rows = []
    for r in run_log.get("screened", []):
        ev = next((e for n, e in evaluated.items() if n in (r["name"], r.get("official_name"))), None)
        rows.append({"name": r["name"], "region": r.get("region"), "eligible": bool(r.get("eligible")),
                     "evaluated": ev is not None, "names": list(dict.fromkeys(x for x in (r["name"], ev and ev["name"]) if x)),
                     "ceo": r.get("ceo"), "founded_date": r.get("founded_date"), "reason": r.get("reason")})
    return rows


def _revenue_cell(bundle: dict | None) -> dict:
    fn = dart.fnltt_revenue(bundle or {})
    au = dart.audit_revenue(bundle or {})
    fn_status = {y: (v or {}).get("status") for y, v in ((bundle or {}).get("fnltt") or {}).items()}
    return {"fnltt": fn, "fnltt_status": fn_status, "audit": au,
            "has_revenue": bool(fn or au)}


def run(run_log_path: str = "outputs/run_log.json") -> dict:
    cfg = get_config()
    log = json.loads((ROOT / run_log_path).read_text(encoding="utf-8"))
    run_date = log.get("run_date") or date.today().isoformat()
    rows, raw = [], {}
    for t in _targets(log):
        group = "평가" if t["evaluated"] else "적격(미평가)" if t["eligible"] else "관문 탈락(참고)"
        row = {"company": t["name"], "group": group, "region": t["region"], "tips_founded": t["founded_date"],
               "tips_ceo": t["ceo"]}
        if t["region"] != "KR":
            rows.append({**row, "status": "해외 — 대상 아님"})
            continue
        s = dart.lookup(t["names"], t["ceo"], t["founded_date"], run_date)
        row["status"] = s["status"]
        row["note"] = s.get("note", "")
        if s["status"] == "matched":
            b = dart.company_bundle(s["corp_code"], run_date)
            raw[s["corp_code"]] = b
            rev = _revenue_cell(b)
            row.update(match=s["match"], identity=s["identity"], corp_code=s["corp_code"], corp_name=s["corp_name"],
                       corp_cls=s["corp_cls"], market=s["market"], listed=s["listed"], est_dt=s["est_dt"],
                       ceo_dart=s["ceo_nm"], filings_5y=s["filings_5y"], audit_reports=s["audit_reports"],
                       latest_audit=s["latest_audit"], revenue=rev)
        rows.append(row)
    kr = [r for r in rows if r["region"] == "KR"]
    target = [r for r in kr if r["group"] != "관문 탈락(참고)"]
    summary = {
        "screened": len(rows), "eligible": sum(r["group"] != "관문 탈락(참고)" for r in rows),
        "evaluated": sum(r["group"] == "평가" for r in rows),
        "domestic_targets": len(target), "found_targets": sum(r["status"] == "matched" for r in target),
        "domestic_reference": len(kr) - len(target),
        "found_reference": sum(r["status"] == "matched" for r in kr if r not in target),
        "listed": [r["company"] for r in kr if r.get("listed")],
        "with_audit_report": [r["company"] for r in kr if r.get("audit_reports")],
        "with_revenue": [r["company"] for r in kr if (r.get("revenue") or {}).get("has_revenue")],
    }
    out = {"run_log": run_log_path, "run_date": run_date, "checked": date.today().isoformat(),
           "cache_dir": f"{cfg.cache.dir}/dart", "method": "corpCode.xml 회사명 정규화 후 정확 일치(없을 때만 기호·공백 무시) → "
           "company.json · list.json(기준일 이전 5년, 전체 / 공시 유형 F) · fnlttSinglAcnt(사업보고서) · 최근 감사보고서 원문 요약",
           "summary": summary, "rows": rows, "raw": raw}
    path(OUT).write_text(dart.redact(json.dumps(out, ensure_ascii=False, indent=1)), encoding="utf-8")
    return out


def _table(out: dict) -> str:
    head = "| 회사 | 구분 | 찾음 여부 | 상장 구분 | 설립일 | 감사보고서 | 매출 수치 |"
    lines = [head, "|" + "---|" * 7]
    for r in out["rows"]:
        if r["region"] != "KR":
            lines.append(f"| {r['company']} | {r['group']} | 해외 — 대상 아님 | - | - | - | - |")
            continue
        if r["status"] != "matched":
            label = {"not_found": "없음", "ambiguous": "동명 여럿", "mismatch": "동명(다른 회사)", "unavailable": "조회 불가"}
            lines.append(f"| {r['company']} | {r['group']} | {label.get(r['status'], r['status'])} | - | - | - | - |")
            continue
        rev = r["revenue"]
        fn = rev["fnltt"]
        au = rev["audit"]
        fn_txt = (f"주요계정 {fn['year']}년 {fn['amount']:,}원" if fn
                  else "주요계정 없음(" + ", ".join(f"{y} {s}" for y, s in sorted(rev["fnltt_status"].items())) + ")")
        au_txt = f"감사보고서 {au['year']}년 {au['amount']:,}원" if au else "감사보고서 매출 없음"
        la = r.get("latest_audit") or {}
        lines.append(f"| {r['company']} | {r['group']} | 찾음({r['match']}, {r['identity']}) | {r['corp_cls']} {r['market']} | "
                     f"{r['est_dt']} | {r['audit_reports']}건{' (' + la.get('report_nm', '') + ')' if la else ''} | "
                     f"{fn_txt} / {au_txt} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", help="DART_API_KEY 가 든 .env 경로 (값은 출력하지 않음)")
    ap.add_argument("--run-log", default="outputs/run_log.json")
    args = ap.parse_args(argv)
    if args.env:
        from dotenv import load_dotenv

        load_dotenv(args.env, override=False)
    print(f"DART 키: {'있음' if os.getenv('DART_API_KEY') else '없음'} · 오프라인: {bool(os.getenv('REPLAY_OFFLINE'))}")
    out = run(args.run_log)
    print(_table(out))
    print(json.dumps(out["summary"], ensure_ascii=False))
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
