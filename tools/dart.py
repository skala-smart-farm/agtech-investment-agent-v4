"""금융감독원 전자공시시스템(DART) OpenAPI 로 국내 후보의 상장 구분·설립일·감사보고서·매출을 확인한다 (무료, 키 DART_API_KEY).

쓰임새 세 가지. 판정을 바꾸는 것은 '공시로 확인된 사실'뿐이고, DART 에 없다는 사실은 어디에도 감점 근거로 쓰지 않는다
(초기 스타트업은 외부감사·사업보고서 대상이 아니라 대부분 DART 에 없다 → '미확인' 그대로).
- (a) 상장 여부 교차 확인: 기업개황 corp_cls 가 Y(유가증권)·K(코스닥)·N(코넥스)면 상장. 적격성 관문 G1(한국거래소 목록 대조)은
      동결 파일이라 그대로 두고, 투자 판단의 경고(scorecard['warnings'])와 보고서 한계점에 '상장 의심'으로 남긴다.
- (b) 창업 시점 t0: 기업개황 est_dt(법인 설립일). 창업자 에이전트가 TIPS 설립일 다음, 국민연금 최초 가입일 앞에 쓰고
      TIPS·국민연금 값과의 차이를 founder['t0_check'] 에 기록한다.
- (c) 실적 근거: 감사보고서(공시 유형 F) 원문 요약의 매출액(TOT_SALES)·손익계산서 매출액 행, 사업보고서 제출 법인은
      단일회사 주요계정(fnlttSinglAcnt) 매출액. 근거 저장소에 'DART' 출처로 등록해 평가표 R1(연도가 적힌 매출액)이 판정에 쓴다.

회사 찾기: corpCode.xml(전체 고유번호 약 12만 건)에서 회사명을 정규화((주)·㈜·주식회사·농업회사법인 등 제거)해 정확히 같은 것만 찾고,
없을 때만 기호·공백까지 무시한 이름으로 찾는다. 같은 이름이 여럿이면 대표자·설립일로 가린다. 국내 법인만 대상이다.

캐시 ({cache.dir}/dart/ — 새 평가는 cache_fresh/, 재현은 replay/):
- corp_codes.json   : 조회한 이름 → 고유번호 후보 (전체 목록 대신 필요한 매핑만 저장, 못 찾은 이름은 빈 목록)
- {corp_code}.json  : 회사별 원본 응답(company.json · list.json · fnlttSinglAcnt) + 최근 감사보고서 원문에서 뽑은 요약값
REPLAY_OFFLINE 이면 캐시만 읽고(없으면 'unavailable'), 키가 없어도 같다. 키 값은 화면·파일·오류 메시지에 남기지 않는다.
"""
from __future__ import annotations

import io
import json
import os
import re
import tempfile
import zipfile
from datetime import date
from functools import lru_cache
from pathlib import Path

from core.config import get_config, path
from tools.grounding import norm
from tools.listing_check import normalize

API = "https://opendart.fss.or.kr/api"
VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"              # 공시 원문 뷰어 (키 불필요)
COMPANY_PAGE = "https://dart.fss.or.kr/dsae001/selectPopup.ax?selectKey={corp_code}"  # 기업개황 (키 불필요)
AGENT = "dart"
MARKETS = {"Y": "유가증권시장", "K": "코스닥", "N": "코넥스", "E": "기타법인(비상장)"}
LISTED = {"Y", "K", "N"}
OK, NO_DATA = "000", "013"            # OpenDART status: 정상, 조회된 데이터 없음
SALES_ACCOUNTS = ("매출액", "수익(매출액)", "영업수익", "매출")
YEARS_BACK = 5                        # 공시 목록 조회 기간 (기준일 이전 5년)
TIMEOUT = 30


# matched 가 아닐 때 후보 기록에 남기는 고정 문구 (숫자를 넣지 않는다 — 보고서 숫자 대조의 입력에 섞이지 않게)
STATUS_NOTE = {
    "not_found": "DART 고유번호 목록에 같은 이름이 없음 (외부감사·공시 의무가 없는 초기 기업이면 정상, 감점 근거 아님)",
    "ambiguous": "같은 이름의 법인이 여럿이라 대표자·설립일로 가리지 못함 (쓰지 않음)",
    "mismatch": "이름만 같고 대표자가 다르며 설립일로도 확인되지 않음 (동명 회사로 보고 쓰지 않음)",
    "unavailable": "조회하지 못함 (키 없음·오프라인 캐시 없음·오류)",
}


class DartUnavailable(RuntimeError):
    """키 없음·오프라인·한도 초과·점검 등으로 조회하지 못함 (캐시하지 않는다)."""


# ── 키 가리기
def redact(text: object) -> str:
    """오류 메시지·로그에서 인증키를 가린다 (requests 예외 문구에는 요청 URL 이 그대로 들어간다)."""
    s = re.sub(r"(crtfc_key=)[^&\s'\"]+", r"\1***", str(text))
    key = os.getenv("DART_API_KEY")
    return s.replace(key, "***") if key else s


def _key() -> str | None:
    return os.getenv("DART_API_KEY") or None


def _offline() -> bool:
    return bool(os.getenv("REPLAY_OFFLINE"))


# ── 캐시 파일
def _dir() -> Path:
    return path(f"{get_config().cache.dir}/dart/.keep").parent


def _read(f: Path) -> dict | None:
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write(f: Path, data: dict) -> None:
    """임시 파일에 다 쓴 뒤 교체한다. 키가 섞여 들어가면 가린다(원본 응답에는 키가 없지만 방어적으로)."""
    text = redact(json.dumps(data, ensure_ascii=False, indent=1))
    f.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f".{f.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(tmp, f)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _codes_file() -> Path:
    return _dir() / "corp_codes.json"


def _company_file(corp_code: str) -> Path:
    return _dir() / f"{corp_code}.json"


# ── API 호출
def _request(endpoint: str, **params) -> bytes:
    if _offline():
        raise DartUnavailable("REPLAY_OFFLINE — 캐시에 없는 DART 조회")
    key = _key()
    if not key:
        raise DartUnavailable("DART_API_KEY 없음")
    import requests

    last = None
    for _ in range(2):  # 연결 오류만 한 번 더
        try:
            r = requests.get(f"{API}/{endpoint}", params={"crtfc_key": key, **params}, timeout=TIMEOUT)
            if r.status_code != 200:
                raise DartUnavailable(f"HTTP {r.status_code}")
            return r.content
        except DartUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — 메시지에 키가 든 URL 이 있으므로 가려서 다시 던진다
            last = redact(f"{type(e).__name__}: {e}")
    raise DartUnavailable(last or "요청 실패")


def _json(endpoint: str, **params) -> dict:
    """OpenDART JSON 응답. 정상(000)·데이터 없음(013)만 돌려주고 그 밖의 상태(키 오류·한도·점검)는 DartUnavailable."""
    try:
        data = json.loads(_request(endpoint, **params).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise DartUnavailable(f"{endpoint} 응답 해석 실패: {redact(e)}") from None
    if data.get("status") not in (OK, NO_DATA):
        raise DartUnavailable(f"{endpoint} status {data.get('status')} {data.get('message', '')}")
    return data


@lru_cache(maxsize=1)
def _corp_index() -> dict[str, list[dict]]:
    """corpCode.xml(zip) 전체 → {정규화 이름: [고유번호 행]}. 한 실행에서 한 번만 받는다 (메모리에만, 파일로 남기지 않음)."""
    import xml.etree.ElementTree as ET

    raw = _request("corpCode.xml")
    if not raw.startswith(b"PK"):
        raise DartUnavailable("corpCode.xml 이 zip 이 아님: " + redact(raw[:200].decode("utf-8", "replace")))
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        root = ET.fromstring(zf.read(zf.namelist()[0]))
    idx: dict[str, list[dict]] = {}
    for el in root.iter("list"):
        row = {k: (el.findtext(k) or "").strip()
               for k in ("corp_code", "corp_name", "corp_eng_name", "stock_code", "modify_date")}
        idx.setdefault(_exact_key(row["corp_name"]), []).append(row)
    return idx


def _exact_key(name: str) -> str:
    """정확 일치 키: (주)·㈜·주식회사·농업회사법인·유한회사·공백만 지운다 (tools.listing_check.normalize)."""
    return normalize(name)


def _loose_key(name: str) -> str:
    """느슨한 키: 정확 일치가 없을 때만 쓴다. 기호(·.-&)·괄호 속 표기까지 지운다."""
    return norm(re.sub(r"\(.*?\)", "", normalize(name)))


def find_corp(names: list[str]) -> dict:
    """회사명(여러 표기)으로 고유번호 후보를 찾는다. 결과는 corp_codes.json 에 이름별로 남긴다.
    반환: {"status": found|not_found|unavailable, "match": exact|loose|None, "rows": [...], "note": str}"""
    keys = [k for k in dict.fromkeys(_exact_key(n) for n in names if n) if len(k) >= 2]
    if not keys:
        return {"status": "not_found", "match": None, "rows": [], "note": "이름 없음"}
    f = _codes_file()
    cache = _read(f) or {"source": "OpenDART corpCode.xml", "names": {}}
    names_c = cache.setdefault("names", {})
    missing = [k for k in keys if k not in names_c]
    note = ""
    if missing:
        try:
            idx = _corp_index()
        except DartUnavailable as e:
            known = [k for k in keys if k in names_c]
            if not known:
                return {"status": "unavailable", "match": None, "rows": [], "note": str(e)}
            keys, note = known, "일부 표기만 캐시로 확인"  # 재현 실행: 캐시에 있는 표기만으로 판단
        else:
            loose: dict[str, list[dict]] | None = None
            for k in missing:
                rows = idx.get(k, [])
                entry = {"exact": rows}
                if not rows:  # 정확 일치가 없을 때만 느슨한 키로
                    if loose is None:
                        loose = {}
                        for ek, rs in idx.items():
                            loose.setdefault(_loose_key(ek), []).extend(rs)
                    entry = {"exact": [], "loose": loose.get(_loose_key(k), [])}
                names_c[k] = entry
            cache["corpcode_date"] = date.today().isoformat()
            _write(f, cache)
    exact = _uniq([r for k in keys for r in (names_c[k].get("exact") or [])])
    if exact:
        return {"status": "found", "match": "exact", "rows": exact, "note": note}
    loose_rows = _uniq([r for k in keys for r in (names_c[k].get("loose") or [])])
    if loose_rows:
        return {"status": "found", "match": "loose", "rows": loose_rows, "note": "기호·공백 무시 일치"}
    return {"status": "not_found", "match": None, "rows": [], "note": note or "고유번호 목록에 같은 이름 없음"}


def _uniq(rows: list[dict]) -> list[dict]:
    return list({r["corp_code"]: r for r in rows}.values())


# ── 회사별 원본 응답
def _iso(d: str | None) -> str | None:
    s = re.sub(r"\D", "", d or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 else None


def _bundle(corp_code: str) -> dict:
    """캐시의 회사 묶음. 없으면 기업개황(company.json)만 받아 부분 묶음(partial)으로 저장한다 — 동명 회사를 가릴 때는 이것만 쓴다."""
    f = _company_file(corp_code)
    if (cached := _read(f)) is not None:
        return cached
    b = {"corp_code": corp_code, "fetched": date.today().isoformat(), "partial": True,
         "company": _json("company.json", corp_code=corp_code)}
    _write(f, b)
    return b


def company_bundle(corp_code: str, run_date: str) -> dict:
    """회사 1곳의 원본 응답 묶음. 캐시에 다 있으면 그대로(기간이 달라도), 기업개황만 있으면 나머지를 받아 저장한다.
    - company : company.json (기업개황: corp_cls·est_dt·ceo_nm …)
    - list_all: list.json 기준일 이전 5년 전체 공시(최종본, 첫 100건) / list_audit: 같은 기간 공시 유형 F(외부감사 관련)
    - fnltt   : {사업연도: fnlttSinglAcnt(사업보고서 11011)} — 최근 사업연도부터 정상 응답이 나올 때까지 2개 연도
    - audit_doc: 최근 감사보고서 원문(document.xml)에서 뽑은 요약값 (원문 zip 은 저장하지 않음)"""
    b = _bundle(corp_code)
    if not b.get("partial"):
        return b
    end = date.fromisoformat(run_date[:10])
    try:
        bgn = end.replace(year=end.year - YEARS_BACK)
    except ValueError:  # 2월 29일
        bgn = end.replace(year=end.year - YEARS_BACK, day=28)
    window = {"bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d")}
    b = {"corp_code": corp_code, "fetched": date.today().isoformat(), "window": window, "company": b["company"],
         "list_all": _json("list.json", corp_code=corp_code, last_reprt_at="Y", page_count=100, **window),
         "list_audit": _json("list.json", corp_code=corp_code, pblntf_ty="F", last_reprt_at="Y", page_count=100,
                             **window),
         "fnltt": {}, "audit_doc": None}
    # 주요계정은 상장사·사업보고서 제출 법인만 나온다. 사업보고서가 없으면 직전 사업연도부터 2개 연도만 확인(대개 013 데이터 없음)
    annual = [x for x in b["list_all"].get("list") or [] if "사업보고서" in x.get("report_nm", "")]
    m = re.search(r"\((\d{4})\.(\d{2})\)", annual[0]["report_nm"]) if annual else None
    y = int(m.group(1)) if m else end.year - 1
    for year in (y, y - 1):
        res = _json("fnlttSinglAcnt.json", corp_code=corp_code, bsns_year=str(year), reprt_code="11011")
        b["fnltt"][str(year)] = res
        if res.get("status") == OK:
            break
    if audit := latest_audit(b):
        raw = _request("document.xml", rcept_no=audit["rcept_no"])
        if not raw.startswith(b"PK"):  # 원문 대신 오류 JSON: 파일 없음(013·014)만 기록하고, 키·한도·점검 오류는 캐시하지 않는다
            try:
                status = json.loads(raw.decode("utf-8")).get("status")
            except (ValueError, UnicodeDecodeError):
                status = None
            if status not in (NO_DATA, "014"):
                raise DartUnavailable(f"document.xml status {status}")
        b["audit_doc"] = {"rcept_no": audit["rcept_no"], "report_nm": audit["report_nm"], **parse_audit_document(raw)}
    _write(_company_file(corp_code), b)
    return b


def latest_audit(b: dict) -> dict | None:
    """최근 감사보고서(개별 우선, 없으면 연결) 목록 행."""
    rows = [x for x in (b.get("list_audit") or {}).get("list") or [] if "감사보고서" in x.get("report_nm", "")]
    rows.sort(key=lambda x: x.get("rcept_dt", ""), reverse=True)
    return next((x for x in rows if "연결" not in x["report_nm"]), rows[0] if rows else None)


# ── 감사보고서 원문 요약
_AMOUNT = re.compile(r"^\(?-?\d{1,3}(?:,\d{3})+\)?$|^\(?-?\d{4,}\)?$")
_UNIT = re.compile(r"단위\s*[:：]\s*(백만원|천원|원)")
UNIT_MULT = {"원": 1, "천원": 1_000, "백만원": 1_000_000}


def _cell_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("&nbsp;", " ").strip()


def _to_int(s: str) -> int:
    neg = s.startswith("(") or s.startswith("-")
    v = int(re.sub(r"\D", "", s))
    return -v if neg else v


def parse_audit_document(raw: bytes) -> dict:
    """document.xml(zip) → 감사보고서 요약값.
    - summary : 문서 머리의 표준 추출값 (TOT_SALES 매출액·TOT_ASSETS 자산총계·TOT_DEBTS 부채총계 — 백만원, TOT_EMPL 종업원 수)
    - sales_row: 손익계산서의 첫 '매출액(영업수익)' 행 {label, current, prior, unit} — 원 단위로 환산
    원문이 형식에 맞지 않으면 빈 값으로 둔다(판정에 쓰지 않음)."""
    out: dict = {"summary": {}, "sales_row": None}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            texts = [zf.read(n).decode("utf-8", "replace") for n in zf.namelist() if n.lower().endswith(".xml")]
    except zipfile.BadZipFile:
        out["error"] = "document.xml 이 zip 이 아님"
        return out
    doc = max(texts, key=len) if texts else ""
    for code in ("TOT_SALES", "TOT_ASSETS", "TOT_DEBTS", "TOT_EMPL"):
        if (m := re.search(rf'<EXTRACTION[^>]*ACODE="{code}"[^>]*>\s*(-?[\d,]+)\s*<', doc)):
            out["summary"][code] = int(m.group(1).replace(",", ""))
    for tr in re.finditer(r"<TR[^>]*>(.*?)</TR>", doc, re.S):
        cells = [_cell_text(c) for c in re.findall(r"<T[DEHU][^>]*>(.*?)</T[DEHU]>", tr.group(1), re.S)]
        if not cells:
            continue
        label = re.sub(r"^[\sⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX0-9.\-()]+", "", cells[0]).replace(" ", "")
        label = re.sub(r"\(주석[\d,\s]*\)$", "", label)
        if label not in SALES_ACCOUNTS:
            continue
        nums = [_to_int(c.replace(" ", "")) for c in cells[1:] if _AMOUNT.match(c.replace(" ", ""))]
        if not nums:
            continue
        units = _UNIT.findall(doc[: tr.start()])
        unit = units[-1] if units else "원"
        mult = UNIT_MULT[unit]
        out["sales_row"] = {"label": cells[0], "unit": unit, "current": nums[0] * mult,
                            "prior": nums[1] * mult if len(nums) > 1 else None}
        break
    return out


def audit_revenue(b: dict) -> dict | None:
    """감사보고서 매출액 {year, amount, prior, basis}. 손익계산서 행이 요약값(TOT_SALES, 백만원)과 맞으면 행의 원 단위 값을,
    행이 없거나 어긋나면 요약값을 쓴다. 매출액이 없거나 0 이하이면 None."""
    a = b.get("audit_doc") or {}
    if not a:
        return None
    m = re.search(r"\((\d{4})\.(\d{2})\)", a.get("report_nm", ""))
    if not m:
        return None
    tot = (a.get("summary") or {}).get("TOT_SALES")
    row = a.get("sales_row") or {}
    cur = row.get("current")
    if cur and (tot is None or abs(cur / 1_000_000 - tot) <= max(1, abs(tot) * 0.005)):
        amount, prior, basis = cur, row.get("prior"), "감사보고서 손익계산서"
    elif tot:
        amount, prior, basis = tot * 1_000_000, None, "감사보고서 요약(매출액, 백만원 단위)"
    else:
        return None
    if amount <= 0:
        return None
    return {"year": int(m.group(1)), "period": f"{m.group(1)}.{m.group(2)}", "amount": amount, "prior": prior,
            "basis": basis}


def fnltt_revenue(b: dict) -> dict | None:
    """사업보고서 주요계정의 매출액 {year, amount, prior, basis, rcept_no, fs}. 개별(OFS) 우선."""
    for year, res in sorted((b.get("fnltt") or {}).items(), reverse=True):
        rows = [x for x in res.get("list") or [] if x.get("account_nm") in SALES_ACCOUNTS
                and x.get("sj_div") in ("IS", "CIS")]
        rows.sort(key=lambda x: x.get("fs_div") != "OFS")
        for x in rows:
            amt = (x.get("thstrm_amount") or "").replace(",", "").strip()
            if re.fullmatch(r"-?\d+", amt) and int(amt) > 0:
                prior = (x.get("frmtrm_amount") or "").replace(",", "").strip()
                return {"year": int(year), "period": (x.get("thstrm_dt") or "").split("~")[-1].strip()[:7],
                        "amount": int(amt), "prior": int(prior) if re.fullmatch(r"-?\d+", prior) else None,
                        "basis": f"사업보고서 주요계정({x.get('fs_nm') or x.get('fs_div')})",
                        "rcept_no": x.get("rcept_no"), "fs": x.get("fs_div")}
    return None


# ── 회사 찾기 + 요약
STRONG = ("대표자 일치", "설립일 일치")
MISMATCH = "대표자 다름"


def _people(s: str | None) -> set[str]:
    return {norm(x) for x in re.split(r"[,/·&]|\s및\s|\s{2,}", s or "") if len(norm(x)) >= 2}


def _same_person(a: str | None, b: str | None) -> bool:
    """대표자 이름 일치 (공동대표 '김준식, 원유현' 은 그중 한 명이면 일치)."""
    return bool(_people(a) & _people(b))


def _identity(company: dict, ceo: str | None, founded: str | None) -> str:
    """같은 회사인지: 대표자 일치 > 설립일 일치 > 이름만 일치(비교할 대표자·설립일이 없음).
    대표자를 아는데 다르고 설립일로도 확인되지 않으면 '대표자 다름' → 동명 회사로 보고 쓰지 않는다 (대표 교체는 설립일로 구제)."""
    est = _iso(company.get("est_dt"))
    if _same_person(ceo, company.get("ceo_nm")):
        return "대표자 일치"
    if founded and est and founded[:10] == est:
        return "설립일 일치"
    if _people(ceo) and _people(company.get("ceo_nm")):
        return MISMATCH
    return "이름만 일치"


def lookup(names: list[str], ceo: str | None = None, founded: str | None = None, run_date: str | None = None) -> dict:
    """회사명(여러 표기)·대표자·설립일(알면)로 DART 회사를 찾아 요약한다. 예외를 던지지 않는다.
    반환 status: matched(같은 회사로 봄) | ambiguous(동명 여러 곳) | mismatch(이름만 같고 대표자·설립일이 다름) |
                not_found(DART 에 없음) | unavailable(키 없음·오프라인 캐시 없음·오류)
    matched 이면 corp_code·corp_name·corp_cls·market·listed·stock_code·est_dt·ceo_nm·identity·filings_5y·
    audit_reports·latest_audit·revenue(최신순)를 함께 준다 (원본 응답은 company_bundle 로 캐시에서 읽는다).
    기호·공백까지 무시해야 같은 이름(loose)이면 대표자·설립일이 맞을 때만 matched 로 본다."""
    from core.config import run_date as default_run_date

    run_date = run_date or default_run_date()
    try:
        found = find_corp(names)
        if found["status"] != "found":
            return {"status": found["status"], "note": found["note"]}
        cands = []
        for row in found["rows"][:5]:  # 동명 회사가 많으면 앞 5곳만 기업개황으로 가린다
            b = _bundle(row["corp_code"])
            cands.append((row["corp_code"], _identity(b.get("company") or {}, ceo, founded)))
        if len(cands) > 1:
            strong = [c for c in cands if c[1] in STRONG]
            if len(strong) != 1:
                return {"status": "ambiguous", "note": f"같은 이름 {len(found['rows'])}곳 — 대표자·설립일로 가리지 못함",
                        "corp_codes": [c[0] for c in cands]}
            cands = strong
        corp_code, ident = cands[0]
        if found["match"] == "loose" and ident not in STRONG:
            return {"status": "ambiguous", "note": "기호·공백을 무시해야 같은 이름이고 대표자·설립일로 확인되지 않음 (쓰지 않음)",
                    "corp_codes": [corp_code]}
        if ident == MISMATCH:
            return {"status": "mismatch", "note": "이름은 같으나 대표자가 다르고 설립일도 일치하지 않음 (동명 회사로 보고 쓰지 않음)",
                    "corp_code": corp_code}
        return summarize(company_bundle(corp_code, run_date), ident, found["match"])
    except DartUnavailable as e:
        return {"status": "unavailable", "note": redact(e)}
    except Exception as e:  # noqa: BLE001 — DART 는 보조 근거라 어떤 오류도 평가를 멈추지 않는다
        return {"status": "unavailable", "note": redact(f"{type(e).__name__}: {e}")}


def summarize(b: dict, identity: str, match: str = "exact") -> dict:
    c = b.get("company") or {}
    cls = c.get("corp_cls") or ""
    audits = [x for x in (b.get("list_audit") or {}).get("list") or [] if "감사보고서" in x.get("report_nm", "")]
    la = latest_audit(b)
    revenue = []
    if (r := audit_revenue(b)) and la:
        revenue.append({**r, "report_nm": la["report_nm"], "rcept_no": la["rcept_no"], "rcept_dt": _iso(la["rcept_dt"]),
                        "flr_nm": la.get("flr_nm")})
    if r := fnltt_revenue(b):
        filing = next((x for x in (b.get("list_all") or {}).get("list") or [] if x.get("rcept_no") == r.get("rcept_no")), {})
        revenue.append({**r, "report_nm": filing.get("report_nm") or f"사업보고서 ({r['year']})",
                        "rcept_dt": _iso(filing.get("rcept_dt")) or _iso(r.get("rcept_no", "")[:8]),
                        "flr_nm": filing.get("flr_nm")})
    revenue.sort(key=lambda x: (x["rcept_dt"] or "", x["year"]), reverse=True)
    return {
        "status": "matched", "match": match, "identity": identity, "corp_code": b["corp_code"],
        "corp_name": re.sub(r"^\s*(?:주식회사|\(주\)|㈜)\s*|\s*(?:주식회사|\(주\)|㈜)\s*$", "", c.get("corp_name") or ""),
        "corp_cls": cls, "market": MARKETS.get(cls, cls or "미상"), "listed": cls in LISTED,
        "stock_code": c.get("stock_code") or "", "est_dt": _iso(c.get("est_dt")), "ceo_nm": c.get("ceo_nm") or "",
        "acc_mt": c.get("acc_mt") or "", "filings_5y": int((b.get("list_all") or {}).get("total_count") or 0),
        "audit_reports": len(audits),
        "latest_audit": ({"report_nm": la["report_nm"], "rcept_no": la["rcept_no"], "rcept_dt": _iso(la["rcept_dt"]),
                          "flr_nm": la.get("flr_nm")} if la else None),
        "revenue": revenue,
    }


# ── 근거 저장소 등록
def _won(v: int) -> str:
    eok = v / 100_000_000
    return f"{v:,}원(약 {eok:,.1f}억원)" if eok >= 1 else f"{v:,}원"


def revenue_quote(s: dict, r: dict) -> str:
    """R1 인용문: '회사명 YYYY년 매출액 N원' — 근거 본문에 그대로 들어 있다."""
    return f"{s['corp_name']} {r['year']}년 매출액 {r['amount']:,}원"


def evidence_items(s: dict) -> list[dict]:
    """matched 요약 → 근거 저장소에 넣을 항목 (SourceRegistry.add_dart 형식). 기업개황 1건 + 매출·감사보고서 공시."""
    if s.get("status") != "matched":
        return []
    name = s["corp_name"]
    stock = f", 종목코드 {s['stock_code']}" if s.get("stock_code") else ""
    items = [{
        "key": f"dart:{s['corp_code']}:company", "url": COMPANY_PAGE.format(corp_code=s["corp_code"]),
        "title": f"{name} 기업개황", "date": None,
        "content": (f"{name} 기업개황(금융감독원 전자공시시스템 DART): 법인 설립일 {s.get('est_dt') or '미상'}, "
                    f"대표자 {s.get('ceo_nm') or '미상'}, 상장 구분 {s['market']}{stock}"
                    + (f", 결산월 {s['acc_mt']}월" if s.get("acc_mt") else "")
                    + f". 최근 {YEARS_BACK}년 공시 {s['filings_5y']}건(감사보고서 {s['audit_reports']}건).")}]
    done = set()
    for r in s.get("revenue") or []:
        prior = f", 전년 {_won(r['prior'])}" if r.get("prior") else ""
        auditor = f", 감사인 {r['flr_nm']}" if r.get("flr_nm") and "감사보고서" in r["report_nm"] else ""
        items.append({
            "key": f"dart:{s['corp_code']}:{r['rcept_no']}", "url": VIEWER.format(rcept_no=r["rcept_no"]),
            "title": f"{name} {r['report_nm']}", "date": r.get("rcept_dt"), "rcept_no": r["rcept_no"],
            "content": (f"{name} {r['report_nm']} — {r.get('rcept_dt') or ''} 공시{auditor}. "
                        f"{revenue_quote(s, r)}{prior} ({r['basis']}, 약 {r['amount'] / 100_000_000:,.1f}억원)."),
            "quote": revenue_quote(s, r), "revenue_year": r["year"]})
        done.add(r["rcept_no"])
    la = s.get("latest_audit")
    if la and la["rcept_no"] not in done:  # 매출액을 뽑지 못한 감사보고서도 '외부감사 재무제표 공시' 사실로 등록
        items.append({
            "key": f"dart:{s['corp_code']}:{la['rcept_no']}", "url": VIEWER.format(rcept_no=la["rcept_no"]),
            "title": f"{name} {la['report_nm']}", "date": la.get("rcept_dt"), "rcept_no": la["rcept_no"],
            "content": (f"{name} {la['report_nm']} — {la.get('rcept_dt') or ''} 공시"
                        + (f", 감사인 {la['flr_nm']}" if la.get("flr_nm") else "") + ". 외부감사를 받은 재무제표가 공시됨.")})
    return items


def register(s: dict, reg, access_date: str) -> dict:
    """matched 요약의 공시를 근거 저장소에 등록하고, 요약에 evidence_ids·company_evidence_id·매출별 evidence_id 를 붙인다."""
    if s.get("status") != "matched":
        return s
    ids, out = [], dict(s)
    rev = [dict(r) for r in s.get("revenue") or []]
    for it in evidence_items(s):
        sid = reg.add_dart(it, AGENT, "DART 공시 조회", access_date)
        ids.append(sid)
        if it["key"].endswith(":company"):
            out["company_evidence_id"] = sid
        for r in rev:
            if it.get("rcept_no") and r["rcept_no"] == it["rcept_no"]:
                r["evidence_id"], r["quote"] = sid, it["quote"]
    out["revenue"], out["evidence_ids"] = rev, list(dict.fromkeys(ids))
    return out


def check(record: dict, reg, run_date: str) -> dict:
    """적격성 기록(국내 후보) 1건 → DART 요약. 이름은 관문 이름·공식명, 대표자·설립일은 관문 기록(TIPS 값)으로 가린다.
    reg 가 있으면(평가 대기열에 들어가는 적격 후보) 공시를 근거 저장소에 등록하고, None 이면(관문 탈락) 요약만 남긴다.
    matched 가 아니면 숫자 없는 짧은 dict({'status', 'note'})만 돌려준다 (보고서 숫자 대조·LLM 입력에 섞이지 않게)."""
    names = [record.get("name"), record.get("official_name")]
    s = lookup([n for n in names if n], record.get("ceo"), record.get("founded_date"), run_date)
    if s["status"] != "matched":
        return {"status": s["status"], "note": STATUS_NOTE.get(s["status"], "")}
    return register(s, reg, run_date) if reg is not None else s


# ── 판정·보고서용 해석
def listed_warning(s: dict | None) -> str | None:
    """(a) corp_cls 가 Y·K·N 이면 '상장 의심' 경고 문장."""
    if not s or s.get("status") != "matched" or not s.get("listed"):
        return None
    stock = f", 종목코드 {s['stock_code']}" if s.get("stock_code") else ""
    weak = " — 이름만 일치해 동명 회사일 수 있음" if s.get("identity") == "이름만 일치" else ""
    return (f"상장 의심: DART 기업개황 상장 구분 {s['market']}(corp_cls {s['corp_cls']}{stock}){weak}. "
            "적격성 관문 G1(한국거래소 목록 대조)은 통과했으므로 비상장 여부를 실사에서 다시 확인해야 한다")


def founding(s: dict | None, until: str | None = None) -> dict | None:
    """(b) 창업 시점 근거: {date, evidence_id}. matched 이고 설립일이 기준일 이전일 때만."""
    if not s or s.get("status") != "matched" or not s.get("est_dt"):
        return None
    if until and s["est_dt"] > until[:10]:
        return None
    return {"date": s["est_dt"], "evidence_id": s.get("company_evidence_id"), "identity": s.get("identity")}
