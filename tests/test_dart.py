"""DART 공시 연동 검사 (tools/dart.py 와 연결 지점). 가짜 OpenDART 응답으로 돌려 키·네트워크가 필요 없다.

- 회사 찾기: (주)·주식회사 정규화 후 정확 일치 우선, 동명 회사는 대표자·설립일로 가림, 필요한 매핑만 캐시
- 캐시 재현: REPLAY_OFFLINE 이면 캐시만 (호출 0건), 캐시에 없으면 unavailable. 키는 파일·오류 메시지에 남지 않는다
- 감사보고서 원문 요약(단위 환산)·사업보고서 주요계정 매출, 상장 의심 경고
- 근거 저장소 등록(W id·웹 kind)과 REFERENCE 기관 보고서 형식
- 연결 지점: 탐색 관문 뒤 dart_check, 창업자 t0 우선순위·차이 기록, 평가표 R1 공시 규칙, 투자 판단 경고, 보고서 한계점
- DART 에 없는 회사는 아무것도 바뀌지 않는다 (감점 근거로 쓰지 않음)
"""
from __future__ import annotations

import io
import json
import re
import zipfile

import pytest
import requests

import core.judge as judge
from core.config import get_config
from core.judge import Answer, Answers, judge_dimension, load_rubric
from tools import dart
from tools.sources import REFERENCE_FORMATS, SourceRegistry, format_reference, reference_group, uses_access_date

KEY = "test-dart-key-0123456789abcdef"
RUN = "2026-09-30"

# ── 가짜 OpenDART
CORPS = [  # (corp_code, corp_name, eng, stock_code)
    ("00000001", "(주)새싹팜", "SAESSAK FARM", " "),        # 비상장 외감 법인: 감사보고서 매출(천원 단위)
    ("00000002", "그린팜", "GREEN FARM A", " "),            # 동명 1
    ("00000003", "그린팜", "GREEN FARM B", " "),            # 동명 2
    ("00000004", "코스닥팜", "KOSDAQ FARM", "123456"),      # 코스닥 상장: 사업보고서 주요계정 매출
    ("00000005", "다른팜", "OTHER FARM", " "),              # 이름만 같은 다른 회사(대표자 다름)
    ("00000006", "푸른-들판", "", " "),                     # 느슨한 일치용 (기호 차이)
]
COMPANY = {
    "00000001": {"corp_name": "주식회사 새싹팜", "ceo_nm": "김새싹", "corp_cls": "E", "stock_code": "", "est_dt": "20210302"},
    "00000002": {"corp_name": "그린팜", "ceo_nm": "이초록", "corp_cls": "E", "stock_code": "", "est_dt": "20150101"},
    "00000003": {"corp_name": "그린팜", "ceo_nm": "박그린", "corp_cls": "E", "stock_code": "", "est_dt": "20200505"},
    "00000004": {"corp_name": "(주)코스닥팜", "ceo_nm": "최상장, 한공동", "corp_cls": "K", "stock_code": "123456",
                 "est_dt": "20100101"},
    "00000005": {"corp_name": "다른팜", "ceo_nm": "정다른", "corp_cls": "E", "stock_code": "", "est_dt": "20000101"},
    "00000006": {"corp_name": "푸른-들판", "ceo_nm": "오기호", "corp_cls": "E", "stock_code": "", "est_dt": "20190101"},
}
AUDIT = {"corp_code": "00000001", "corp_name": "새싹팜", "stock_code": "", "corp_cls": "E", "report_nm": "감사보고서 (2025.12)",
         "rcept_no": "20260320000001", "flr_nm": "가나회계법인", "rcept_dt": "20260320", "rm": ""}
ANNUAL = {"corp_code": "00000004", "corp_name": "코스닥팜", "stock_code": "123456", "corp_cls": "K",
          "report_nm": "사업보고서 (2025.12)", "rcept_no": "20260315000004", "flr_nm": "코스닥팜", "rcept_dt": "20260315",
          "rm": ""}


def _fnltt_row(fs: str, amount: str) -> dict:
    return {"rcept_no": "20260315000004", "reprt_code": "11011", "bsns_year": "2025", "corp_code": "00000004",
            "fs_div": fs, "fs_nm": "연결재무제표" if fs == "CFS" else "재무제표", "sj_div": "IS", "sj_nm": "손익계산서",
            "account_nm": "매출액", "thstrm_nm": "제 16 기", "thstrm_dt": "2025.01.01 ~ 2025.12.31", "thstrm_amount": amount,
            "frmtrm_amount": "2,500,000,000", "currency": "KRW"}


def _corp_zip() -> bytes:
    rows = "".join(f"<list><corp_code>{c}</corp_code><corp_name>{n}</corp_name><corp_eng_name>{e}</corp_eng_name>"
                   f"<stock_code>{s}</stock_code><modify_date>20250101</modify_date></list>" for c, n, e, s in CORPS)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", f'<?xml version="1.0" encoding="UTF-8"?><result>{rows}</result>')
    return buf.getvalue()


def _doc_zip(unit: str = "천원", row: tuple = ("Ⅰ. 매출액(주석 5)", "5", "1,234,567", "987,654"), tot: int | None = 1235) -> bytes:
    summary = f'<EXTRACTION ACODE="TOT_SALES" AFEATURE="BOTH">{tot}</EXTRACTION>' if tot is not None else ""
    cells = "".join(f'<TD ALIGN="RIGHT">{c}</TD>' for c in row)
    xml = (f'<?xml version="1.0" encoding="utf-8"?><DOCUMENT><DOCUMENT-NAME ACODE="00760">감사보고서</DOCUMENT-NAME>'
           f'<SUMMARY>{summary}<EXTRACTION ACODE="TOT_EMPL" AFEATURE="BOTH">12</EXTRACTION></SUMMARY>'
           f'<P>손 익 계 산 서</P><P>(단위 : {unit})</P><TABLE><TR><TD>과 목</TD><TD>주석</TD><TD>당기</TD><TD>전기</TD></TR>'
           f'<TR ACOPY="Y">{cells}</TR><TR><TD>Ⅱ. 매출원가</TD><TD></TD><TD>1,000</TD><TD>900</TD></TR></TABLE></DOCUMENT>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("20260320000001_00760.xml", xml)
    return buf.getvalue()


def _respond(endpoint: str, p: dict) -> bytes:
    code = p.get("corp_code")
    if endpoint == "corpCode.xml":
        return _corp_zip()
    if endpoint == "document.xml":
        return _doc_zip()
    none = {"status": "013", "message": "조회된 데이타가 없습니다."}
    if endpoint == "company.json":
        out = {"status": "000", "message": "정상", "corp_code": code, "acc_mt": "12", **COMPANY[code]}
    elif endpoint == "list.json":
        rows = {"00000001": [AUDIT], "00000004": [] if p.get("pblntf_ty") == "F" else [ANNUAL]}.get(code) or []
        out = ({"status": "000", "message": "정상", "page_no": 1, "page_count": 100, "total_count": len(rows),
                "total_page": 1, "list": rows} if rows else none)
    elif endpoint == "fnlttSinglAcnt.json":
        out = ({"status": "000", "message": "정상", "list": [_fnltt_row("CFS", "5,000,000,000"),
                                                             _fnltt_row("OFS", "3,000,000,000")]}
               if code == "00000004" and p["bsns_year"] == "2025" else none)
    else:
        raise AssertionError(endpoint)
    return json.dumps(out, ensure_ascii=False).encode("utf-8")


class _Resp:
    def __init__(self, content: bytes):
        self.status_code, self.content = 200, content


@pytest.fixture
def api(monkeypatch, tmp_path, set_cfg):
    """온라인 + 가짜 응답. calls 에 (endpoint, params) 를 남긴다 (키는 params 에서 뺀 뒤 기록)."""
    set_cfg("cache.dir", str(tmp_path / "cache"))
    monkeypatch.delenv("REPLAY_OFFLINE", raising=False)
    monkeypatch.setenv("DART_API_KEY", KEY)
    dart._corp_index.cache_clear()
    calls: list[tuple[str, dict]] = []

    def get(url, params=None, timeout=None, **kw):
        params = dict(params or {})
        assert params.pop("crtfc_key") == KEY and url.startswith(dart.API)
        endpoint = url.rsplit("/", 1)[1]
        calls.append((endpoint, params))
        return _Resp(_respond(endpoint, params))

    monkeypatch.setattr(requests, "get", get)
    yield calls
    dart._corp_index.cache_clear()


def _cache_text(tmp_path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "cache" / "dart").glob("*.json"))


# ── 회사 찾기·캐시
def test_exact_match_raw_cache_and_offline_replay(api, monkeypatch, tmp_path):
    s = dart.lookup(["새싹팜 주식회사"], ceo="김새싹", run_date=RUN)
    assert (s["status"], s["match"], s["identity"]) == ("matched", "exact", "대표자 일치")
    assert (s["corp_code"], s["corp_name"], s["corp_cls"], s["market"], s["listed"], s["est_dt"]) == \
        ("00000001", "새싹팜", "E", "기타법인(비상장)", False, "2021-03-02")
    assert s["audit_reports"] == 1 and s["latest_audit"]["rcept_dt"] == "2026-03-20"
    rev = s["revenue"][0]
    assert (rev["year"], rev["amount"], rev["prior"]) == (2025, 1_234_567_000, 987_654_000)   # 천원 → 원
    assert "손익계산서" in rev["basis"] and rev["rcept_no"] == "20260320000001"
    # 캐시: 조회한 이름의 매핑만 (전체 고유번호 목록은 저장하지 않음) + 회사별 원본 응답. 키는 어디에도 없다
    codes = json.loads((tmp_path / "cache/dart/corp_codes.json").read_text(encoding="utf-8"))
    assert set(codes["names"]) == {"새싹팜"} and "코스닥팜" not in _cache_text(tmp_path)
    bundle = json.loads((tmp_path / "cache/dart/00000001.json").read_text(encoding="utf-8"))
    assert bundle["company"]["status"] == "000" and bundle["list_audit"]["list"][0]["rcept_no"] == "20260320000001"
    assert bundle["fnltt"] == {"2024": {"status": "013", "message": "조회된 데이타가 없습니다."},
                               "2025": {"status": "013", "message": "조회된 데이타가 없습니다."}}
    assert bundle["window"] == {"bgn_de": "20210930", "end_de": "20260930"}           # 기준일 이전 5년
    assert bundle["audit_doc"]["summary"]["TOT_SALES"] == 1235
    assert KEY not in _cache_text(tmp_path)
    n = len(api)
    # 재현 실행: 오프라인·키 없음 → 캐시만으로 같은 결과, API 호출 0건
    monkeypatch.setenv("REPLAY_OFFLINE", "1")
    monkeypatch.delenv("DART_API_KEY")
    dart._corp_index.cache_clear()
    assert dart.lookup(["새싹팜 주식회사"], ceo="김새싹", run_date=RUN) == s
    assert dart.lookup(["(주)새싹팜", "새로생긴표기"], ceo="김새싹", run_date=RUN)["corp_code"] == "00000001"  # 캐시에 있는 표기로
    assert len(api) == n
    assert dart.lookup(["처음보는회사"], run_date=RUN)["status"] == "unavailable"


def test_not_found_homonyms_mismatch_loose(api):
    assert dart.lookup(["없는회사"], run_date=RUN)["status"] == "not_found"
    amb = dart.lookup(["그린팜"], run_date=RUN)
    assert amb["status"] == "ambiguous" and amb["corp_codes"] == ["00000002", "00000003"]
    assert dart.lookup(["그린팜"], ceo="박그린", run_date=RUN)["corp_code"] == "00000003"          # 대표자로 가림
    # 동명 가리기에는 기업개황만 부른다 (고른 회사만 공시 목록·주요계정)
    assert sum(e == "list.json" and p["corp_code"] == "00000002" for e, p in api) == 0
    assert dart.lookup(["그린팜"], founded="2015-01-01", run_date=RUN)["corp_code"] == "00000002"  # 설립일로 가림
    assert dart.lookup(["다른팜"], ceo="홍길동", run_date=RUN)["status"] == "mismatch"             # 이름만 같은 다른 회사
    fixed = dart.lookup(["다른팜"], ceo="홍길동", founded="2000-01-01", run_date=RUN)                # 대표 교체는 설립일로 구제
    assert fixed["status"] == "matched" and fixed["identity"] == "설립일 일치"
    # 정확 일치가 없을 때만 기호·공백 무시. 이때는 대표자·설립일이 맞아야 같은 회사로 본다
    assert dart.lookup(["푸른 들판"], run_date=RUN)["status"] == "ambiguous"
    loose = dart.lookup(["푸른 들판"], founded="2019-01-01", run_date=RUN)
    assert loose["status"] == "matched" and loose["match"] == "loose" and loose["corp_code"] == "00000006"
    # 관문 기록용 요약: DART 에 없으면 숫자 없는 고정 문구만, 근거 등록 없음
    reg = SourceRegistry()
    out = dart.check({"name": "없는회사", "official_name": "(주)없는회사", "region": "KR"}, reg, RUN)
    assert out == {"status": "not_found", "note": dart.STATUS_NOTE["not_found"]} and reg.data == {}
    assert not re.search(r"\d", json.dumps(out, ensure_ascii=False))


def test_listed_company_fnltt_revenue_and_warning(api):
    s = dart.lookup(["코스닥팜"], ceo="한공동", run_date=RUN)    # 공동대표 중 한 명이면 대표자 일치
    assert s["identity"] == "대표자 일치" and s["listed"] and s["market"] == "코스닥" and s["stock_code"] == "123456"
    r = s["revenue"][0]
    assert (r["amount"], r["prior"], r["fs"], r["year"]) == (3_000_000_000, 2_500_000_000, "OFS", 2025)  # 개별 우선
    assert (r["rcept_no"], r["rcept_dt"], r["report_nm"]) == ("20260315000004", "2026-03-15", "사업보고서 (2025.12)")
    w = dart.listed_warning(s)
    assert w.startswith("상장 의심") and "코스닥" in w and "123456" in w and "동명" not in w
    assert "동명 회사일 수 있음" in dart.listed_warning({**s, "identity": "이름만 일치"})
    assert dart.listed_warning(dart.lookup(["새싹팜"], ceo="김새싹", run_date=RUN)) is None   # 기타법인(E)
    assert dart.listed_warning({"status": "not_found"}) is None and dart.listed_warning(None) is None


def test_key_is_redacted_from_errors(monkeypatch, tmp_path, set_cfg):
    set_cfg("cache.dir", str(tmp_path / "cache"))
    monkeypatch.delenv("REPLAY_OFFLINE", raising=False)
    monkeypatch.setenv("DART_API_KEY", KEY)
    dart._corp_index.cache_clear()

    def boom(url, params=None, timeout=None, **kw):  # requests 예외 문구에는 키가 든 요청 URL 이 그대로 들어간다
        raise requests.ConnectionError(f"Max retries exceeded with url: /api/x?crtfc_key={params['crtfc_key']}&corp_code=1")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(dart.DartUnavailable) as e:
        dart._request("company.json", corp_code="1")
    assert KEY not in str(e.value) and "crtfc_key=***" in str(e.value)
    s = dart.lookup(["새싹팜"], run_date=RUN)
    assert s["status"] == "unavailable" and KEY not in json.dumps(s, ensure_ascii=False)
    assert dart.redact(f"https://x/api?crtfc_key={KEY}&a=1 {KEY}") == "https://x/api?crtfc_key=***&a=1 ***"
    monkeypatch.delenv("DART_API_KEY")
    assert dart.lookup(["새싹팜"], run_date=RUN)["status"] == "unavailable"   # 키가 없으면 호출 없이
    dart._corp_index.cache_clear()


# ── 감사보고서 원문 요약
def test_parse_audit_document_units_and_fallback():
    a = dart.parse_audit_document(_doc_zip())
    assert a["summary"] == {"TOT_SALES": 1235, "TOT_EMPL": 12}
    assert a["sales_row"] == {"label": "Ⅰ. 매출액(주석 5)", "unit": "천원", "current": 1_234_567_000, "prior": 987_654_000}
    r = dart.audit_revenue({"audit_doc": {"report_nm": "감사보고서 (2025.12)", **a}})
    assert (r["year"], r["period"], r["amount"]) == (2025, "2025.12", 1_234_567_000)
    # 손익계산서 행이 요약값(백만원)과 어긋나면 요약값을 쓴다
    b = dart.parse_audit_document(_doc_zip(unit="원", row=("매 출 액", "", "1,234,567", "1,000")))
    r = dart.audit_revenue({"audit_doc": {"report_nm": "감사보고서 (2025.12)", **b}})
    assert r["amount"] == 1_235_000_000 and "요약" in r["basis"] and r["prior"] is None
    # 영업수익 표기도 매출로 본다
    c = dart.parse_audit_document(_doc_zip(unit="백만원", row=("Ⅰ. 영업수익", "", "1,235", "900"), tot=None))
    assert c["sales_row"]["current"] == 1_235_000_000
    assert dart.parse_audit_document(b"not a zip")["sales_row"] is None
    assert dart.audit_revenue({"audit_doc": {"report_nm": "감사보고서 (2025.12)", "summary": {"TOT_SALES": 0},
                                             "sales_row": None}}) is None


@pytest.mark.parametrize("status, cached", [("014", True), ("020", False)])
def test_audit_document_error_json(api, monkeypatch, tmp_path, status, cached):
    """감사보고서 원문 대신 오류 JSON: 파일 없음(014)은 '감사보고서 공시 사실'만 남기고 캐시, 한도 초과(020) 같은 일시 오류는
    캐시하지 않아 다음 실행에서 다시 받는다."""
    real = requests.get

    def get(url, params=None, timeout=None, **kw):
        if url.endswith("/document.xml"):
            return _Resp(json.dumps({"status": status, "message": "오류"}).encode("utf-8"))
        return real(url, params=params, timeout=timeout)

    monkeypatch.setattr(requests, "get", get)
    s = dart.lookup(["새싹팜"], ceo="김새싹", run_date=RUN)
    bundle = json.loads((tmp_path / "cache/dart/00000001.json").read_text(encoding="utf-8"))
    if cached:
        assert s["status"] == "matched" and s["revenue"] == [] and s["audit_reports"] == 1
        assert bundle["audit_doc"]["error"] and not bundle.get("partial")
        items = dart.evidence_items(s)
        assert [i["title"] for i in items] == ["새싹팜 기업개황", "새싹팜 감사보고서 (2025.12)"]
        assert "외부감사를 받은 재무제표가 공시됨" in items[1]["content"] and "quote" not in items[1]
    else:
        assert s["status"] == "unavailable" and "020" in s["note"] and bundle.get("partial")


# ── 근거 저장소·REFERENCE
def test_register_evidence_and_reference_format(api):
    s = dart.lookup(["새싹팜"], ceo="김새싹", run_date=RUN)
    reg = SourceRegistry()
    out = dart.register(s, reg, RUN)
    comp, audit = (reg.get(i) for i in out["evidence_ids"])
    assert all(i.startswith("W") for i in out["evidence_ids"]) and comp["kind"] == audit["kind"] == "web"
    assert comp["dart"] and audit["site"] == "금융감독원 전자공시시스템" and audit["date"] == "2026-03-20"
    assert out["company_evidence_id"] == comp["id"] and out["revenue"][0]["evidence_id"] == audit["id"]
    assert format_reference(audit) == ("금융감독원 전자공시시스템(2026). 새싹팜 감사보고서 (2025.12). "
                                       "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260320000001")
    assert format_reference(comp) == ("금융감독원 전자공시시스템(2026). 새싹팜 기업개황. "
                                      "https://dart.fss.or.kr/dsae001/selectPopup.ax?selectKey=00000001")
    for x in (comp, audit):
        assert reference_group(x) == "기관 보고서" and REFERENCE_FORMATS["기관 보고서"].match(format_reference(x))
    assert uses_access_date(comp) and not uses_access_date(audit)   # 기업개황은 접수일이 없어 조회 연도
    quote = out["revenue"][0]["quote"]
    assert quote == "새싹팜 2025년 매출액 1,234,567,000원" and quote in reg.text(audit["id"])
    assert "설립일 2021-03-02" in reg.text(comp["id"]) and "대표자 김새싹" in reg.text(comp["id"])
    again = dart.register(s, reg, RUN)                               # 같은 공시는 한 번만 등록
    assert again["evidence_ids"] == out["evidence_ids"] and len(reg.data) == 2


# ── 연결 지점
def _matched(name: str, cls: str = "E", est: str = "2020-04-17", rcept_dt: str = "2026-03-20",
             corp_code: str = "00000001") -> dict:
    """lookup 이 돌려주는 matched 요약 (API 없이)."""
    return {"status": "matched", "match": "exact", "identity": "대표자 일치", "corp_code": corp_code, "corp_name": name,
            "corp_cls": cls, "market": dart.MARKETS[cls], "listed": cls in dart.LISTED,
            "stock_code": "123456" if cls in dart.LISTED else "", "est_dt": est, "ceo_nm": "김대표", "acc_mt": "12",
            "filings_5y": 1, "audit_reports": 1,
            "latest_audit": {"report_nm": "감사보고서 (2025.12)", "rcept_no": f"{rcept_dt.replace('-', '')}000001",
                             "rcept_dt": rcept_dt, "flr_nm": "가나회계법인"},
            "revenue": [{"year": int(rcept_dt[:4]) - 1, "period": f"{int(rcept_dt[:4]) - 1}.12", "amount": 1_234_567_000,
                         "prior": None, "basis": "감사보고서 손익계산서", "report_nm": "감사보고서 (2025.12)",
                         "rcept_no": f"{rcept_dt.replace('-', '')}000001", "rcept_dt": rcept_dt, "flr_nm": "가나회계법인"}]}


def test_dart_check_after_screen(monkeypatch):
    from graph import discovery_graph as dg

    seen = []

    def fake_lookup(names, ceo=None, founded=None, run_date=None):
        seen.append((tuple(names), ceo, founded, run_date))
        if "에이팜" in names:
            return _matched("에이팜", cls="K")
        if "비팜" in names:
            return _matched("비팜", corp_code="00000002")
        return {"status": "not_found", "note": "고유번호 목록에 같은 이름 없음"}

    monkeypatch.setattr(dart, "lookup", fake_lookup)
    recs = [{"name": "에이팜", "official_name": "(주)에이팜", "region": "KR", "eligible": True, "reason": "통과",
             "ceo": "김대표", "founded_date": "2020-04-17"},
            {"name": "비팜", "region": "KR", "eligible": False, "reason": "G5 AgTech 분야가 아님"},
            {"name": "씨팜", "region": "KR", "eligible": True, "reason": "통과"},
            {"name": "Global Farm", "region": "GLOBAL", "eligible": True, "reason": "통과"}]

    def screen(state):
        return {"screened": [dict(r) for r in recs], "queue": [*state["queue"], *(dict(r) for r in recs if r["eligible"])],
                "registry": {"W1": {"id": "W1"}}, "log": ["[관문]"]}

    out = dg.dart_check(screen)({"run_date": RUN, "queue": [{"name": "이전 후보"}], "registry": {}})
    by = {r["name"]: r for r in out["screened"]}
    assert [(r["eligible"], r["reason"]) for r in out["screened"]] == [(r["eligible"], r["reason"]) for r in recs]
    assert by["에이팜"]["dart"]["status"] == "matched" and len(by["에이팜"]["dart"]["evidence_ids"]) == 2
    assert by["씨팜"]["dart"] == {"status": "not_found", "note": dart.STATUS_NOTE["not_found"]}
    assert "dart" not in by["Global Farm"]                                  # 해외 후보는 대상 아님
    assert by["비팜"]["dart"]["status"] == "matched" and "evidence_ids" not in by["비팜"]["dart"]  # 탈락 후보는 등록 안 함
    assert set(out["registry"]) == {"W1", *by["에이팜"]["dart"]["evidence_ids"]}
    assert out["queue"][0] == {"name": "이전 후보"} and out["queue"][1]["dart"]["listed"]
    assert [c["name"] for c in out["queue"]] == ["이전 후보", "에이팜", "씨팜", "Global Farm"]
    assert seen[0] == (("에이팜", "(주)에이팜"), "김대표", "2020-04-17", RUN)
    assert out["log"][0] == "[관문]" and out["log"][-1].startswith("[DART] 국내 3곳 조회")
    assert "상장 의심: 에이팜" in out["log"][-1]
    ok = {"screened": [{"name": "G", "region": "GLOBAL"}], "log": []}
    assert dg.dart_check(lambda state: ok)({"queue": []}) is ok              # 국내 후보가 없으면 출력 그대로
    from agents.eligibility import eligibility_node

    sub = dg.build_discovery_graph()
    assert sub.builder.nodes["screen"].runnable.func.__wrapped__ is eligibility_node   # 관문 함수는 v1 그대로 부른다


def _kr(**kw) -> dict:
    c = {"name": "테스트팜", "official_name": "테스트팜", "region": "KR", "founded_year": 2020, "founded_date": None,
         "nps": {"status": "matched", "first_date": "2020-06-01", "evidence_id": "Wnps01"}}
    c.update(kw)
    return c


def test_t0_priority_and_cross_check():
    import agents.founder as fd

    reg, names = SourceRegistry(), ["테스트팜"]
    d = {**_matched("테스트팜"), "company_evidence_id": "Wdart1"}
    # TIPS 설립일이 있으면 그대로 t0, DART 와의 차이는 기록만
    c = _kr(founded_date="2020-04-10", dart=d)
    t = fd.pick_t0(c, reg, [], names, RUN)
    assert (t["t0"], t["t0_basis"]) == ("2020-04-10", "TIPS 설립일")
    chk = fd.t0_cross_check(c, t, RUN)
    assert chk["dart_est_dt"] == "2020-04-17" and [x["days_vs_dart"] for x in chk["compare"]] == [-7, 45]
    assert "7일 빠름" in chk["notes"][0] and "45일 늦음" in chk["notes"][1] and "첫 고용" in chk["notes"][1]
    # TIPS 가 없으면 DART 법인 설립일이 국민연금 최초 가입일보다 앞선다
    c = _kr(dart=d)
    t = fd.pick_t0(c, reg, [], names, RUN)
    assert (t["t0"], t["t0_basis"], t["t0_evidence_ids"]) == ("2020-04-17", "DART 설립일", ["Wdart1"])
    assert "Wdart1" in t["t0_source"] and "이름만 일치" not in t["t0_source"]
    t = fd.pick_t0(_kr(dart={**d, "identity": "이름만 일치"}), reg, [], names, RUN)
    assert "이름만 일치" in t["t0_source"]
    # DART 에 없거나(not_found) 쓰지 않는 결과면 이전 규칙 그대로, 기록도 없음 → 창업자 결과·프롬프트가 바뀌지 않는다
    for x in ({"status": "not_found", "note": dart.STATUS_NOTE["not_found"]}, None, {"status": "mismatch"},
              {**d, "est_dt": "2026-12-01"}):   # 기준일 이후 설립일도 쓰지 않음
        c = _kr(dart=x)
        t = fd.pick_t0(c, reg, [], names, RUN)
        assert (t["t0"], t["t0_basis"]) == ("2020-06-01", "국민연금 최초 가입일") and fd.t0_cross_check(c, t, RUN) is None


class _FakeJudge:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return Answers(answers=[Answer(**a) for a in self.replies.pop(0)])


def _traction(r1: dict) -> list[dict]:
    rest = [{"qid": q, "verdict": "UNKNOWN", "quote": "", "evidence_ids": [], "rationale": "근거 없음"}
            for q in ("R2", "R3", "R4")]
    return [{"qid": "R1", "evidence_ids": ["W1"], **r1}, *rest]


def test_r1_rule_uses_dart_revenue(monkeypatch):
    base = SourceRegistry()
    w1 = base.add_web({"url": "https://news.example.com/a/1", "title": "팜랩 소식", "published_date": "2026-05-01",
                       "content": "팜랩은 스마트팜 제어기를 만든다. 팜랩은 아직 매출이 없다고 밝혔다."}, "tech", "q", RUN)
    company = {"name": "팜랩", "official_name": "팜랩", "stage": "Seed", "stage_evidence_ids": []}

    def run(dart_info, r1, pool_extra=True, rcept_dt="2026-03-20"):
        reg = SourceRegistry(dict(base.data))
        d = dart.register(dart_info, reg, RUN) if dart_info else None
        pool = [w1] + ((d or {}).get("evidence_ids") or [] if pool_extra else [])
        monkeypatch.setattr(judge, "bounded", lambda schema, role="generator", kind="default": _FakeJudge([_traction(r1)]))
        res = judge_dimension("traction", {**company, **({"dart": d} if d else {})}, pool, reg, "[후보] 팜랩", RUN)
        return next(r for r in res["rows"] if r["qid"] == "R1"), d

    unknown = {"verdict": "UNKNOWN", "quote": "", "rationale": "매출 근거 없음"}
    row, d = run(_matched("팜랩"), unknown)
    assert row["answer"] == "YES" and row["x"] == 1 and row["evidence_ids"] == [d["revenue"][0]["evidence_id"]]
    assert row["quote"] == "팜랩 2025년 매출액 1,234,567,000원" and "코드 판정, DART" in row["rationale"]
    assert "LLM 판정: 매출 근거 없음" in row["rationale"] and row["confidence"] in ("상", "중")
    # 공시가 오래됐으면(접수 24개월 초과) 그대로 미확인
    assert run(_matched("팜랩", rcept_dt="2024-03-20"), unknown)[0]["answer"] == "UNKNOWN"
    # 공시 근거가 판정 근거 풀에 없으면 쓰지 않는다
    assert run(_matched("팜랩"), unknown, pool_extra=False)[0]["answer"] == "UNKNOWN"
    # DART 에 없는 회사: 미확인 그대로 (DART 에 없다는 사실을 감점에 쓰지 않음)
    row, _ = run(None, unknown)
    assert row["answer"] == "UNKNOWN" and "DART" not in row["rationale"]
    # LLM 이 반대 근거로 NO 를 냈으면 바꾸지 않는다
    no = {"verdict": "NO", "quote": "팜랩은 아직 매출이 없다고 밝혔다", "rationale": "매출이 없다고 밝힘"}
    assert run(_matched("팜랩"), no)[0]["answer"] == "NO"


def _all_unknown(dim_id: str) -> dict:
    d = next(x for x in load_rubric()["dimensions"] if x["id"] == dim_id)
    rows = [{"dim": dim_id, "qid": q["id"], "short": q["short"], "point": q["point"], "question": q["text"],
             "bessemer_q": q["bessemer_q"], "answer": "UNKNOWN", "x": 0, "evidence_ids": [], "quote": "",
             "rationale": "근거 없음", "confidence": None} for q in d["questions"]]
    return {"dim": dim_id, "name": d["name"], "weight": d["weight"], "owner": d["owner"], "rows": rows, "yes": 0,
            "no": 0, "unknown": len(rows), "na": 0, "n": len(rows), "rejected_yes": [], "quote_retried": 0}


def test_decision_warning_and_dart_pool(monkeypatch):
    from agents import decision as dec

    pools = {}

    def fake(dim_id, company, pool_ids, reg, analysis, run_date):
        pools[dim_id] = list(pool_ids)
        return _all_unknown(dim_id)

    monkeypatch.setattr(dec, "judge_dimension", fake)
    reg = SourceRegistry({"W1": {"id": "W1", "kind": "web", "url": "https://n.example.com/1"}})
    listed = dart.register(_matched("에이팜", cls="K"), reg, RUN)
    current = {"name": "에이팜", "official_name": "에이팜", "region": "KR", "stage": "Seed", "segment_id": "greenhouse",
               "round_date": "2026-01", "round_amount": "10억원", "stage_evidence_ids": ["W1"], "evidence_ids": ["W1"],
               "one_line": "AI 온실 제어", "dart": listed}
    st = {"current": current, "registry": reg.data, "run_date": RUN, "iterations": 1, "founder": {}, "tech": {},
          "market": {}, "competition": {}}
    out = dec.decision_node(st)
    sc = out["scorecard"]
    assert sc["warnings"][0].startswith("상장 의심") and "코스닥" in sc["warnings"][0]
    assert pools["traction"] == ["W1", *listed["evidence_ids"]] and "경고: 상장 의심(DART)" in out["log"][0]
    # DART 에 없는 회사: 경고·추가 근거 없음 (점수·결정도 같다)
    out2 = dec.decision_node({**st, "current": {**current, "dart": {"status": "not_found", "note": "-"}}})
    assert "warnings" not in out2["scorecard"] and pools["traction"] == ["W1"]
    assert (out2["decision"], out2["scorecard"]["multiplier"]) == (out["decision"], sc["multiplier"])


def test_report_limitation_line():
    from agents import report as rep

    evals = [{"name": "에이팜", "scorecard": {"warnings": ["상장 의심: DART 기업개황 상장 구분 코스닥(corp_cls K)"]},
              "founder": {"t0_check": {"t0": "2020-04-10", "t0_basis": "TIPS 설립일",
                                       "notes": ["TIPS 설립일 2020-04-10 은 DART 설립일 2020-04-17 보다 7일 빠름",
                                                 "국민연금 최초 가입일 2020-04-17 = DART 설립일"]}}},
             {"name": "비팜", "scorecard": {}, "founder": {}}]
    notes = rep._dart_notes(evals)
    assert len(notes) == 1 and notes[0].startswith("DART(금융감독원 전자공시시스템) 교차 확인: 에이팜 — 상장 의심")
    assert "7일 빠름" in notes[0] and "= DART" not in notes[0] and "비팜" not in notes[0]
    assert rep._dart_notes([evals[1], {"name": "씨팜", "scorecard": {}, "founder": {"t0_check": {"notes": [
        "TIPS 설립일 2020-04-17 = DART 설립일"]}}}]) == []        # 차이가 없거나 DART 에 없으면 한 줄도 없다
    cfg = get_config()
    pool = {"ref_gap": None, "unevaluated": [], "evaluated": 2, "eligible": 2, "why": ""}
    base = rep._limitations(cfg, pool, {}, [], "hold", [])
    assert rep._limitations(cfg, pool, {}, [], "hold", [], []) == base
    assert rep._limitations(cfg, pool, {}, [], "hold", [], notes)[0] == notes[0]


def test_step1_table_reproduces_from_replay_cache(monkeypatch, tmp_path):
    """저장소의 재현 캐시(replay/dart/)만으로 1단계 표를 다시 만든다: 국내 적격·평가 후보 10곳은 DART 에 없고,
    관문에서 탈락한 대동애그테크(대조군)만 찾힌다 (기타법인·설립 2022-02-15·감사보고서 2025.12 매출)."""
    from eval import dart_check

    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.setattr(dart_check, "OUT", str(tmp_path / "dart_check.json"))
    out = dart_check.run("outputs/run_log.json")
    s = out["summary"]
    assert (s["eligible"], s["evaluated"], s["domestic_targets"], s["found_targets"]) == (14, 10, 10, 0)
    rows = {r["company"]: r for r in out["rows"]}
    assert all(r["status"] == "not_found" for r in rows.values() if r["region"] == "KR" and r["group"] != "관문 탈락(참고)")
    dd = rows["대동애그테크"]
    assert (dd["status"], dd["corp_cls"], dd["est_dt"], dd["identity"]) == ("matched", "E", "2022-02-15", "대표자 일치")
    assert dd["revenue"]["audit"]["year"] == 2025 and dd["revenue"]["fnltt"] is None
    assert "01919963" in out["raw"] and (tmp_path / "dart_check.json").exists()
