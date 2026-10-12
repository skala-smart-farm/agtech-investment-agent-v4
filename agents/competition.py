"""🥊 경쟁사 비교 에이전트 (Scorecard '경쟁 우위 10%', 문항 C1~C4).

기술 요약 결과(제품·핵심 기술·회사 측 차별점 주장 claims)를 받아, 국내·해외 경쟁사와 실제로 비교해 차별성과 진입장벽을 검증한다.
- 경쟁사는 분야 이름("애그테크")이 아니라 대상의 **제품 유형**(예: 온실 과채류 수확 로봇)으로 찾는다.
  LLM 이 제품 설명에서 제품 유형과 검색 질의를 정하고(검색 계획), 에이전트가 그 질의로 검색한다.
- 대상 회사 기사 본문에 경쟁사로 직접 언급된 회사(예: "영국 더그투스 등 … 외국 경쟁사")는 코드로 뽑아 반드시 검토한다.
- 경쟁사 행마다 근거 id 를 달고, 이름이 근거에 없는 경쟁사는 버린다. 대상의 우위는 단정하지 않고 "회사 측 주장"으로 쓴다.
- 기술 요약의 차별점 주장(claims)마다 '제3자 확인'·'회사 주장'·'반대 근거'를 붙인다(verified_claims).
  근거 id 가 없거나 회사 자체 사이트 근거뿐인 '제3자 확인', 근거 id 가 없는 '반대 근거'는 코드가 '회사 주장'으로 낮춘다.
v3 경쟁사 실재 확인 (평가표 C2 '국내외 경쟁사 2곳 이상과 비교')
- 같은 세부 분야(segment_id)의 다른 적격 후보(이번 실행 적격성 관문 통과, state['screened'])를 경쟁사 후보로 먼저 보여 준다.
  관문 G6 이 서로 다른 출처 3곳 이상(제3자 1곳 이상)으로 실재를 확인한 회사라 '확인'으로 둔다(추가 검색 없음).
- 웹 근거에서 찾은 경쟁사는 회사명이 서로 다른 도메인 competition.min_domains(2)곳 이상에서 나오고, 그중 1곳 이상이
  대상 회사 기사 밖 출처일 때만 '확인'. 모자라면 회사명 검색을 경쟁사 1곳당 1회, 평가 후보 1곳당 competition.verify_max_searches
  회까지 더 한다. 그래도 모자라면 '근거 약함' — 비교표에는 남기되 확인된 경쟁사 수(confirmed_competitors)에는 세지 않는다.
- confirmed_competitors 가 rubric C2 의 min_competitors(2) 미만이면 core.judge 가 C2 의 YES 를 UNKNOWN 으로 내린다.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from agents.tech import candidate_line, evidence_blocks, judge_own
from core.config import get_config, get_segment
from core.llm import structured
from core.llm_bounded import bounded
from core.prompts import render
from tools.grounding import norm
from tools.sources import SourceRegistry, naming_domains
from tools.web_search import web_search

AGENT = "competition"
MAX_QUERIES = 4  # 검색 계획 질의 상한 (+ "{회사} 경쟁사" 1건)
# v3 경쟁사 실재 확인 기본값 (config.yaml competition 이 있으면 그 값)
DEFAULTS = {"max_peers": 5, "min_domains": 2, "verify_max_searches": 4}
CONFIRMED, WEAK = "확인", "근거 약함"
DOMESTIC = re.compile(r"한국|대한민국|국내|korea", re.I)

RIVAL_TERMS = re.compile(r"경쟁|competitor|rival|대비|비교|점유율|market share", re.I)  # 본문 문단 고르기용
RIVAL = re.compile(r"경쟁\s?(?:사|업체|기업)|competitors?|rivals?", re.I)                  # 경쟁사를 말하는 문장
_COUNTRY = "영국|미국|네덜란드|이스라엘|일본|중국|독일|프랑스|호주|스페인|벨기에|캐나다|이탈리아|덴마크|노르웨이|스웨덴|대만|싱가포르|인도"
_NAME = r"[가-힣A-Za-z][가-힣A-Za-z0-9&\-]{1,20}"
# 경쟁 문장 안의 회사 이름: "영국 더그투스", "더그투스 등 … 경쟁사", "경쟁사인 ○○", "competitors such as Tevel"
MENTION = [re.compile(rf"(?:{_COUNTRY})의?\s(?P<name>{_NAME})"),
           re.compile(rf"(?<![가-힣A-Za-z])(?P<name>{_NAME})\s?등\s?[^.]{{0,30}}?경쟁\s?(?:사|업체|기업)"),
           re.compile(rf"경쟁\s?(?:사|업체|기업)(?:인|로는|으로는)\s(?P<name>{_NAME}(?:\s?,\s?{_NAME}){{0,4}})"),
           re.compile(r"(?:competitors?|rivals?)(?: such as| like| including)? (?P<name>[A-Z][\w&\-]+(?: [A-Z][\w&\-]+)?"
                      r"(?:(?:, | and )[A-Z][\w&\-]+(?: [A-Z][\w&\-]+)?){0,4})")]
LIST_SEP = re.compile(r"\s?,\s?|\s(?:and|or)\s")
NOT_COMPANY = {"시장", "기업", "업체", "회사", "스타트업", "경쟁사", "경쟁", "제품", "기술", "정부", "농가", "농업", "외국", "해외",
               "국내", "업계", "로봇", "등", "대비", "비슷한", "유사한", "다른", "기존", "현지", "The", "Other", "Many",
               "진출", "수출", "작물", "딸기", "토마토", "오이", "파프리카", "과일", "채소", "서비스", "솔루션", "플랫폼", "대표",
               "한국", "정부", "전시회", "박람회", "CES", "투자", "고객", "농장", "온실", "스마트팜"}
# 이름 끝의 조사 (한글·영문 이름 모두: '테벨과', 'Tevel보다', '시장에')
PARTICLE = re.compile(r"(?<=[가-힣A-Za-z]{2})(?:보다|에서|에게|으로|와|과|의|는|은|를|을|도|로|가|이|에)$")
# 대상이 경쟁사보다 낫다는 단정 (검증 전에는 쓰지 않는다). '회사 측 주장:' 뒤의 말은 출처를 밝힌 것이라 허용.
# '선도 기업'·'앞서 언급한' 같은 경쟁사 사실 서술은 단정이 아니므로 비교 표현만 잡는다
SUPERIOR = re.compile(r"앞선다|앞서 있|앞서는|우위(?:에|를|가)|우월|뛰어나|능가|압도|더 낫|차별화된다|outperform|superior|ahead of",
                      re.I)
CLAIMED = re.compile(r"회사\s?측\s?주장\s?[:：][^/\n.]*")  # 주장은 문장 끝('.')이나 '/' 까지
CITE = re.compile(r"\[[WD][0-9a-f]{5,8}")
# 경쟁사 이름 키에서 뗄 법인 접미사 ('Dogtooth Technologies Ltd' → 'Dogtooth', '㈜트랙팜' → '트랙팜')
SUFFIX = re.compile(r"(?:[\s,]+(?:inc|ltd|llc|plc|corp|corporation|co|gmbh|b\.?v|s\.?a|ag|technologies|technology)\.?)+\s*$"
                    r"|^\s*(?:\(주\)|㈜|주식회사)\s*|\s*(?:\(주\)|㈜|주식회사)\s*$", re.I)
# 이름 키가 되면 아무 글에나 맞는 말 ('Tevel (이스라엘)' 의 '이스라엘', 'Dogtooth, Inc.' 의 'inc')
NOT_KEY = ({norm(x) for x in _COUNTRY.split("|")} | {norm(x) for x in NOT_COMPANY}
           | {"inc", "ltd", "llc", "plc", "corp", "corporation", "company", "gmbh", "technologies", "technology", "robotics",
              "주식회사", "주", "대한민국", "korea", "uk", "usa", "us"})


def rival_keys(*names: str | None) -> list[str]:
    """회사 이름 표기들 → 정규화한 이름 키 (근거 본문 대조용).
    괄호·슬래시·쉼표로 나뉜 표기마다 원래 꼴과 법인 접미사를 뗀 꼴을 넣는다. 영문은 3글자, 한글은 2글자 이상이고
    나라 이름·일반어는 뺀다. 예) '더그투스(Dogtooth Technologies)' → ['더그투스', 'dogtoothtechnologies', 'dogtooth']"""
    out: list[str] = []
    for n in names:
        for part in re.split(r"[()/,·]", n or ""):
            for v in (part, SUFFIX.sub("", part)):
                k = norm(v)
                if len(k) >= (3 if k.isascii() else 2) and k not in NOT_KEY and k not in out:
                    out.append(k)
    return out


def _overlap(a: list[str], b: list[str]) -> bool:
    """이름 키 두 묶음이 같은 회사를 가리키는지 (같거나, 3글자 이상에서 한쪽이 다른 쪽을 포함)."""
    return any(x == y or (min(len(x), len(y)) >= 3 and (x in y or y in x)) for x in a for y in b)


def _plain(s: str | None) -> str:
    """근거 id 표기([W..])를 뺀 문장 (검색 계획 입력용)."""
    return re.sub(r"\[[^\]]*\]", "", s or "").strip()


def _asserts(text: str) -> bool:
    """출처를 밝히지 않은 우열 단정이 있으면 True. '회사 측 주장: …' 부분(그 문장 끝까지)은 빼고 나머지만 본다."""
    return bool(SUPERIOR.search(CLAIMED.sub("", text or "")))


def mentioned_competitors(reg: SourceRegistry, ids: list[str], keys: list[str]) -> list[dict]:
    """대상 회사가 나오는 근거 본문에서 '경쟁' 문장에 이름이 나온 회사 → [{name, evidence_ids, quote}]."""
    nk = [norm(k) for k in keys if k and len(norm(k)) >= 2]
    found: dict[str, dict] = {}
    for sid in dict.fromkeys(ids):
        text = reg.text(sid)
        if not text or not any(k in norm(text) for k in nk):
            continue
        for sent in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text)):
            if not RIVAL.search(sent):
                continue
            for pat in MENTION:
                for part in (p for m in pat.finditer(sent) for p in LIST_SEP.split(m.group("name"))):
                    nm = PARTICLE.sub("", part.strip())
                    n_ = norm(nm)
                    if nm in NOT_COMPANY or len(n_) < 2 or any(k in n_ or n_ in k for k in nk):
                        continue
                    row = found.setdefault(n_, {"name": nm, "evidence_ids": [], "quote": sent.strip()[:200]})
                    if sid not in row["evidence_ids"]:
                        row["evidence_ids"].append(sid)
    return list(found.values())


class SearchPlan(BaseModel):
    product_type_ko: str = Field(description="대상 제품 유형 (예: '온실 과채류 수확·수분 로봇')")
    product_type_en: str = Field(description="영문 제품 유형 (예: 'greenhouse fruit harvesting robot')")
    queries: list[str] = Field(description="경쟁사 검색 질의 2~4개 (국내 1개 이상·해외 1개 이상, 언급된 경쟁사 이름 포함)")


class Competitor(BaseModel):
    name: str
    name_en: str = Field(description="영문 표기 (해외 회사는 원문 영문명, 국내 회사는 근거에 나온 영문명). 모르면 빈 문자열")
    country: str
    offering: str = Field(description="제품·접근 방식 (근거 id)")
    scale: str = Field(description="투자 단계·매출·고객 규모 등 알려진 규모, 모르면 '확인 불가'")
    vs_target: str = Field(description="중립 비교 한 문장: 경쟁사 사실 [근거 id] / 대상은 '회사 측 주장: ~' [근거 id]")
    evidence_ids: list[str] = Field(description="이 경쟁사 이름이 실제로 나오는 근거 id (1개 이상)")


class VerifiedClaim(BaseModel):
    claim: str = Field(description="기술 요약이 정리한 차별점 주장 (문구 그대로, 근거 id 표기는 빼도 됨)")
    status: Literal["제3자 확인", "회사 주장", "반대 근거"] = Field(
        description="제3자 확인: 회사가 아닌 출처(기관·언론 취재·실증 결과·경쟁사 비교)가 확인 / "
                    "회사 주장: 회사 발표·대표 발언뿐 / 반대 근거: 경쟁사가 같은 기능을 이미 제공하는 등 주장과 어긋나는 근거가 있음")
    evidence_ids: list[str] = Field(description="판단에 쓴 근거 id (제3자 확인·반대 근거는 필수)")


class CompetitionAnalysis(BaseModel):
    competitors: list[Competitor] = Field(description="3~5곳")
    differentiation: str = Field(description="대상의 차별성 판단: 제3자 근거로 확인된 것과 회사 측 주장을 구분 (근거 id)")
    entry_barriers: str = Field(description="특허·데이터·네트워크 효과·인증 등 진입장벽 (근거 id), 약하면 약하다고 쓴다")
    threats: list[str] = Field(description="경쟁 위협 (근거 id)")
    verified_claims: list[VerifiedClaim] = Field(description="기술 요약의 차별점 주장마다 1개 (주장이 없으면 빈 목록)")
    evidence_ids: list[str]


def _ground(res: CompetitionAnalysis, ids: list[str], reg: SourceRegistry, named: list[dict]) -> list[dict]:
    """이름이 근거에 실제로 나오는 경쟁사만 남기고, 행마다 근거 id 를 맞춘다 (지어낸 경쟁사 차단).
    LLM 이 근거 id 를 비우면 대상 기사 속 언급(named) → 나머지 근거 순으로 채운다."""
    named_ids = {norm(m["name"]): m["evidence_ids"] for m in named}
    rows = []
    for c in res.competitors:
        keys = rival_keys(c.name)  # 나라 이름('Tevel (이스라엘)')·법인 접미사만으로 근거가 맞지 않게
        found = [i for i in ids if any(k in norm(reg.text(i)) for k in keys)]
        first = [i for k in keys for i in named_ids.get(k, []) if i in found]
        ev = [i for i in c.evidence_ids if i in found] or list(dict.fromkeys(first + found))[:3]
        if not ev:
            continue
        row = {**c.model_dump(), "evidence_ids": ev}
        if _asserts(row["vs_target"]):
            row["vs_target"] = "회사 측 주장(제3자 비교 근거 없음): " + row["vs_target"]
        if not CITE.search(row["vs_target"]):  # 표 칸에도 출처가 이어지게
            row["vs_target"] += f" [{', '.join(ev[:2])}]"
        rows.append(row)
    return rows


def _own_site(s: dict | None, keys: list[str]) -> bool:
    """회사 자체 사이트 근거인지 (호스트에 회사 영문명·이름이 들어감)."""
    url = (s or {}).get("url") or ""
    host = norm(url.split("/")[2]) if url.count("/") >= 2 else ""
    return bool(host) and any(k in host for k in keys)


def verify_claims(items: list[VerifiedClaim], valid: set[str], reg: SourceRegistry, company: list[str]) -> list[dict]:
    """주장 검증 결과를 코드로 점검한다. 근거 id 는 저장소에 있는 것만 남기고,
    - '제3자 확인'인데 근거 id 가 없거나 회사 자체 사이트 근거뿐이면 → '회사 주장'
    - '반대 근거'인데 근거 id 가 없으면 → '회사 주장'
    낮춘 항목은 downgraded=True 로 표시한다."""
    keys = [norm(k) for k in company if k and len(norm(k)) >= 3]
    out = []
    for v in items:
        ev = [i for i in dict.fromkeys(v.evidence_ids) if i in valid]
        third = [i for i in ev if not _own_site(reg.get(i), keys)]
        low = (v.status == "제3자 확인" and not third) or (v.status == "반대 근거" and not ev)
        out.append({"claim": v.claim, "status": "회사 주장" if low else v.status, "evidence_ids": ev, "downgraded": low})
    return out


def _settings() -> dict:
    """config.yaml competition (없는 키는 DEFAULTS)."""
    return {**DEFAULTS, **dict(get_config().get("competition") or {})}


def _own_or_db(s: dict, keys: list[str]) -> bool:
    """회사 자체 사이트이거나 기업 DB 프로필(제3자 보도가 아님)인지 — 비교 근거로 보여 줄 근거를 고를 때 뒤로 미룬다."""
    return _own_site(s, keys) or (s.get("site") in ("THE VC", "혁신의숲"))


def _firsts(doms: dict[str, list[str]], reg: SourceRegistry, keys: list[str]) -> list[str]:
    """출처 도메인마다 첫 근거 id. 제3자 기사를 먼저, 회사 자체 사이트·기업 DB 를 뒤에 둔다 (정렬은 안정적)."""
    return sorted((ids[0] for ids in doms.values()), key=lambda i: _own_or_db(reg.get(i), keys))


def same_segment_peers(state: dict, reg: SourceRegistry, limit: int) -> list[dict]:
    """이번 실행 적격성 관문 기록(state['screened'])에서 대상과 같은 세부 분야(segment_id)인 다른 적격 후보 → 경쟁사 후보.
    관문 G6 이 서로 다른 출처 3곳 이상(제3자 1곳 이상)으로 실재를 확인한 회사들이다. 후보마다
    {name, region, country, one_line, keys, domains(회사명이 나온 출처 도메인 수), all_ids(관문 근거),
     evidence_ids(LLM 에 보여 줄 근거 최대 2개: 서로 다른 도메인, 제3자 기사 먼저)}. 세부 분야가 없으면 [].
    근거 저장소에서 회사명이 나오는 근거를 하나도 찾지 못한 후보는 인용할 수 없어 뺀다."""
    c = state["current"]
    if not c.get("segment_id"):
        return []
    seg = get_segment(c["segment_id"])["id"]
    own = rival_keys(c.get("official_name"), c.get("name"), c.get("name_en"))
    out: list[dict] = []
    for r in state.get("screened") or []:
        if not r.get("eligible") or not r.get("segment_id") or get_segment(r["segment_id"])["id"] != seg:
            continue
        keys = rival_keys(r.get("official_name"), r.get("name"), r.get("name_en"))
        if not keys or _overlap(keys, own) or any(_overlap(keys, p["keys"]) for p in out):
            continue
        all_ids = [i for i in dict.fromkeys(r.get("evidence_ids") or []) if reg.get(i)]
        doms = naming_domains(reg, all_ids, keys)
        if not doms:
            continue
        shown = _firsts(doms, reg, keys)[:2]
        out.append({"name": r.get("official_name") or r["name"], "region": r.get("region"),
                    "country": "한국" if r.get("region") == "KR" else "해외", "one_line": r.get("one_line") or "",
                    "keys": keys, "domains": len(doms), "all_ids": all_ids, "evidence_ids": shown})
        if len(out) >= limit:
            break
    return out


def _confirmed(doms: dict[str, list[str]], reg: SourceRegistry, target: list[str], min_domains: int) -> bool:
    """출처 도메인이 min_domains 곳 이상이고, 그중 1곳 이상은 대상 회사 이름이 나오지 않는 출처(대상 회사 기사 밖)인지."""
    if len(doms) < min_domains:
        return False
    return any(not any(k in norm(reg.text(i)) for k in target) for ids in doms.values() for i in ids)


def _verify_query(row: dict, plan: "SearchPlan") -> str:
    """실재 확인 검색어 '"회사명" 제품 유형'. 국내 회사(또는 영문 표기가 없는 회사)는 한글 이름 + 한국어 제품 유형,
    해외 회사는 영문 이름 + 영문 제품 유형 (해외 회사는 국내 기사에 음차로만 나와 영문 검색이 독립 출처를 잘 찾는다)."""
    parts = [SUFFIX.sub("", p).strip() or p.strip()
             for p in [*re.split(r"[()/,·]", row["name"]), row.get("name_en") or ""] if p.strip()]
    parts = [p for p in parts if norm(p) not in NOT_KEY and len(norm(p)) >= (3 if norm(p).isascii() else 2)]
    ko = next((p for p in parts if not p.isascii()), "")
    en = next((p for p in parts if p.isascii()), "")
    if ko and (_domestic(row.get("country")) or not en):
        return f"\"{ko}\" {plan.product_type_ko}".strip()
    return f"\"{en or row['name']}\" {plan.product_type_en}".strip()


def confirm_rivals(rows: list[dict], peers: list[dict], ids: list[str], reg: SourceRegistry, plan: "SearchPlan",
                   target: list[str], min_domains: int, max_searches: int) -> tuple[list[dict], list[str], list[str]]:
    """경쟁사 행마다 실재 확인 표시를 붙인다. 반환 (확인된 행을 앞에 둔 행 목록, 추가 검색 질의, 추가 검색 근거 id).
    - 같은 분야 적격 후보와 같은 회사면 verified '확인', basis '적격성 관문 통과' (검색 없음)
    - 그 밖(웹 근거·대상 기사 속 언급)은 회사명이 나온 출처 도메인 수(n_sources)를 센다. min_domains 곳 이상이고 그중 1곳 이상이
      대상 회사 기사 밖이면 '확인'. 아니면 회사명 검색을 1회(전체 max_searches 회까지) 더 하고 다시 센다. 그래도 아니면 '근거 약함'
    행에 붙는 키: name_en · verified · n_sources · source_ids(도메인마다 첫 근거, 최대 3개) · basis · peer"""
    queries: list[str] = []
    extra: list[str] = []
    for row in rows:
        keys = rival_keys(row["name"], row.get("name_en"))
        peer = next((p for p in peers if _overlap(keys, p["keys"])), None)
        if peer:
            doms = naming_domains(reg, peer["all_ids"], peer["keys"])
            row.update(verified=CONFIRMED, n_sources=len(doms), source_ids=_firsts(doms, reg, peer["keys"])[:3],
                       basis="적격성 관문 통과(같은 세부 분야 후보)", peer=True)
            continue
        doms = naming_domains(reg, ids + extra, keys)
        if not _confirmed(doms, reg, target, min_domains) and len(queries) < max_searches:
            q = _verify_query(row, plan)
            if q not in queries:
                queries.append(q)
                extra += [i for i in web_search(q, reg, AGENT, topic="general", recent=False) if i not in extra]
                doms = naming_domains(reg, ids + extra, keys)
        ok = _confirmed(doms, reg, target, min_domains)
        why = (f"서로 다른 도메인 {len(doms)}곳" if ok else
               f"출처 도메인 {len(doms)}곳" if len(doms) < min_domains else "대상 회사 기사에서만 언급")
        row.update(verified=CONFIRMED if ok else WEAK, n_sources=len(doms),
                   source_ids=_firsts(doms, reg, keys)[:3], basis=why, peer=False)
    ranked = [r for r in rows if r["verified"] == CONFIRMED] + [r for r in rows if r["verified"] != CONFIRMED]
    return ranked, queries, extra


def _domestic(country: str | None) -> bool:
    return bool(DOMESTIC.search(country or ""))


def _analysis_text(c: dict, tech: dict, k: dict) -> str:
    """경쟁 우위 판정에 넘기는 요약문."""
    rows = "; ".join(f"{r['name']}({r['country']}, {r.get('verified') or '확인 표시 없음'}·출처 {r.get('n_sources', '-')}곳): "
                     f"{r['offering']}" for r in k["competitors"])
    claims = "; ".join(f"{v['claim']} → {v['status']}" for v in k["verified_claims"])
    conf = [r for r in k["competitors"] if r.get("verified") == CONFIRMED]
    return "\n".join([
        candidate_line(c),
        f"[기술 요약] 제품: {tech.get('product') or '확인 불가'} / 핵심 기술: {tech.get('core_technology') or '확인 불가'} "
        f"/ 특허·인증: {tech.get('ip_evidence') or '확인 불가'}",
        f"[경쟁] 제품 유형: {k['search_plan']['product_type_ko']} / 경쟁사: {rows or '없음'}",
        f"[확인된 경쟁사] {len(conf)}곳 (국내 {sum(_domestic(r['country']) for r in conf)}·해외 "
        f"{sum(not _domestic(r['country']) for r in conf)}) / 근거 약함 {len(k['competitors']) - len(conf)}곳",
        f"[차별성] {k['differentiation']} / 진입장벽: {k['entry_barriers']}",
        f"[차별점 주장 검증] {claims or '검증할 주장 없음'}",
    ])


def competition_node(state: dict) -> dict:
    c, tech = state["current"], state.get("tech", {})
    reg = SourceRegistry(state.get("registry"))
    seg = get_segment(c["segment_id"])
    name = c["official_name"]
    names = [name, c.get("name_en") or ""]
    # 1) 대상 회사 근거 본문에 경쟁사로 직접 언급된 회사 (적격성 근거 + 창업자·기술 요약 근거 전체, v1 기술·팀 근거와 같은 범위)
    own = list(dict.fromkeys(c.get("evidence_ids", []) + state.get("founder", {}).get("pool_ids", [])
                             + tech.get("pool_ids", [])))
    named = mentioned_competitors(reg, own, names)
    # 2) 검색 계획: 제품 유형을 정하고 그 유형의 경쟁사를 찾는 질의를 LLM 이 만든다
    plan: SearchPlan = structured(SearchPlan).invoke(render(
        "competition_plan", name=name, segment=seg["name"], product=_plain(tech.get("product")),
        core_technology=_plain(tech.get("core_technology")),
        named="\n".join(f"- {m['name']}: \"{m['quote']}\"" for m in named) or "(없음)"))
    queries = list(dict.fromkeys(q.strip() for q in plan.queries if q.strip()))[:MAX_QUERIES]
    ids = []
    for q in queries:
        ids += web_search(q, reg, AGENT, topic="general", recent=False)
    ids += web_search(f"{name} 경쟁사", reg, AGENT, topic="news", recent=False, raw=True)
    ids = list(dict.fromkeys(ids + tech.get("evidence_ids", []) + [i for m in named for i in m["evidence_ids"]]))
    # 3) 같은 세부 분야의 다른 적격 후보 (적격성 관문이 실재를 확인한 회사) → 경쟁사 후보로 먼저 보여 준다
    cfg = _settings()
    peers = same_segment_peers(state, reg, int(cfg["max_peers"]))
    peer_ids = [i for p in peers for i in p["evidence_ids"] if i not in ids]
    # 4) 근거: 스니펫 + 대상·경쟁사 이름이 나오는 기사 본문의 '경쟁·비교' 문단 (+ 같은 분야 후보의 관문 근거 요약)
    keys = names + [m["name"] for m in named]
    evidence = "\n\n".join([*evidence_blocks(reg, ids, keys, terms=RIVAL_TERMS, max_chars=600, boost=None).values(),
                             *([reg.brief(peer_ids, 500)] if peer_ids else [])])
    ctx = dict(name=name, segment=seg["name"], product=tech.get("product", ""), product_type=plan.product_type_ko,
               claims="\n".join(f"- {x}" for x in tech.get("claims", [])) or "(없음)",
               named="\n".join(f"- {m['name']} [{', '.join(m['evidence_ids'])}]: \"{m['quote']}\"" for m in named)
               or "(없음)",
               peers="\n".join(f"- {p['name']} ({p['country']}) [{', '.join(p['evidence_ids'])}]: "
                               f"{p['one_line'] or '요약 없음'}" for p in peers) or "(없음)",
               evidence=evidence)
    llm = bounded(CompetitionAnalysis)
    res: CompetitionAnalysis = llm.invoke(render("competition", **ctx, feedback=""))
    bad = [x.name for x in res.competitors if _asserts(x.vs_target)] + (
        ["differentiation"] if _asserts(res.differentiation) else [])
    if bad:  # 우열 단정이 있으면 한 번만 고쳐 쓰게 한다 (남으면 _ground 가 '회사 측 주장'으로 감싼다)
        res = llm.invoke(render("competition", **ctx, feedback=(
            f"다음 칸에 '앞선다·우위·뛰어나다' 같은 우열 단정이 있다: {', '.join(bad)}. "
            "경쟁사는 근거에 적힌 사실로, 대상의 강점은 '회사 측 주장: ~' 으로 고쳐 써라.")))
    out = res.model_dump()
    seen = ids + peer_ids
    target = rival_keys(*names, c.get("name"))
    grounded = [r for r in _ground(res, seen, reg, named) if not _overlap(rival_keys(r["name"]), target)]  # 대상 자신 제외
    # 5) 실재 확인: 같은 분야 적격 후보는 '확인', 웹에서 찾은 경쟁사는 서로 다른 도메인 수를 세고 모자라면 회사명 검색
    rows, verify_q, verify_ids = confirm_rivals(grounded, peers, seen, reg, plan, target, int(cfg["min_domains"]),
                                                int(cfg["verify_max_searches"]))
    out["competitors"] = rows
    if _asserts(out["differentiation"]):
        out["differentiation"] = "회사 측 주장(제3자 비교 근거 없음): " + out["differentiation"]
    known = set(seen)
    out["evidence_ids"] = [i for i in dict.fromkeys(res.evidence_ids + [i for r in out["competitors"]
                                                                        for i in r["evidence_ids"]]) if i in known]
    out["pool_ids"] = ids  # 판정 근거 풀: 같은 분야 후보·실재 확인 검색 근거는 넣지 않는다 (다른 회사 기사가 문항 근거를 밀어내지 않게)
    out["search_plan"] = {"product_type_ko": plan.product_type_ko, "product_type_en": plan.product_type_en,
                          "queries": queries}
    out["mentioned_competitors"] = named
    conf = [r for r in rows if r["verified"] == CONFIRMED]
    out["confirmed_competitors"] = len(conf)  # core.judge 가 C2(min_competitors) 검사에 쓴다
    out["rival_check"] = {
        "min_domains": int(cfg["min_domains"]), "peers": [p["name"] for p in peers],
        "peers_used": [r["name"] for r in rows if r.get("peer")], "queries": verify_q, "evidence_ids": verify_ids,
        "confirmed": [r["name"] for r in conf], "weak": [r["name"] for r in rows if r["verified"] != CONFIRMED]}
    out["verified_claims"] = verify_claims(res.verified_claims, set(ids), reg, names)
    out["evidence_ids"] = [i for i in dict.fromkeys(out["evidence_ids"] + [i for v in out["verified_claims"]
                                                                          for i in v["evidence_ids"]])]
    out["criterion"] = judge_own("competition", {**state, "competition": out}, reg, ids, _analysis_text(c, tech, out))
    crit = out["criterion"]
    status = [v["status"] for v in out["verified_claims"]]
    msg = (f"[경쟁사] {name}: 제품 유형 '{plan.product_type_ko}', 검색 {len(queries) + 1}건"
           f"{f' + 실재 확인 {len(verify_q)}건' if verify_q else ''}, "
           f"근거 속 경쟁사 {', '.join(m['name'] for m in named) or '없음'}, 같은 분야 적격 후보 {len(peers)}곳 "
           f"→ 경쟁사 {len(rows)}곳 비교(확인 {len(conf)}곳: 국내 {sum(_domestic(r['country']) for r in conf)}·해외 "
           f"{sum(not _domestic(r['country']) for r in conf)}, 근거 약함 {len(rows) - len(conf)}곳)"
           f"{' (우열 단정 고쳐 씀)' if bad else ''}, 주장 {len(status)}건(제3자 확인 {status.count('제3자 확인')}·"
           f"반대 근거 {status.count('반대 근거')}) → 경쟁 우위 YES {crit['yes']}/{crit['n']}")
    print(msg)
    return {"registry": reg.data, "competition": out, "log": [msg]}
