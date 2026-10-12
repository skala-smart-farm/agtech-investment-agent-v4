"""평가표 문항 판정 (모든 분석 에이전트와 투자 판단이 함께 쓴다).

계약 C3. 에이전트마다 자기 Scorecard 기준(차원) 하나를 judge_dimension 으로 판정해 state[키]['criterion'] 에 둔다.
- founder → 창업자, market → 시장성, tech → 제품/기술력, competition → 경쟁 우위, decide → 실적·투자조건
  (rubric.yaml 차원의 owner 필드). decide 는 criterion 이 없는 차원도 여기서 보완 판정한다.

LLM 은 평가표 질문에 판정(YES/NO/UNKNOWN/N/A)·근거 id·근거 원문 인용만 답한다. 점수와 결론은 agents/decision.py 의 코드가 정한다.
판정 코드와 프롬프트(prompts/decision.md)는 v1 agents/decision.py 의 차원 판정 루프를 동작 변경 없이 옮긴 것이다
(v1 제출 실행 10곳×24문항을 재현 캐시로 다시 판정해 판정·근거 id·인용·이유가 모두 같음을 확인. 근거 풀이 빈 경우만 새로 처리).
낙관 편향을 막는 장치
- 항목(차원)별로 따로 호출한다 (한 항목의 인상이 다른 항목으로 번지는 후광 효과 방지)
- 점수·기준점·"유망" 같은 표현을 LLM 에 보여 주지 않는다
- YES 는 인용문이 실제 근거 본문에 있어야 인정한다 (코드가 문자열로 검사). 실패하면 UNKNOWN 으로 강등
- NO 도 반대 사실을 적은 문장의 인용이 본문에 있어야 인정한다. "근거가 없다"는 NO 가 아니라 UNKNOWN
  (판정 이유가 "추정·확인되지 않음·근거가 없음" 같은 근거 부족이고 인용 자체에 부정 표현이 없으면 코드가 UNKNOWN 으로 바꾼다)
- 인용 앞뒤 300자 안에 회사명(또는 v3: 회사 제품·서비스 이름)이 있어야 한다 (시장 문항 M1·M2 제외, 업계 일반론 차단)
- 제3자 근거·최근 24개월(문서는 발행 연도)·문서 근거 요구 조건을 코드가 검사한다. 최근 24개월 = [기준일 − 24개월, 기준일]이라
  기준일 이후 날짜(예정·계획)는 최근으로 인정하지 않는다 (규칙은 core/recency.py 한 곳). 최근 문항(F3·R1·R2)은 인용 속 날짜가
  기준일 이후뿐이거나 인용이 완료 표지 없는 예정·계획·목표 문장이면 인정하지 않는다 (시장 전망 M1·M2 는 제외)
  (제3자 문항은 기사에 실렸어도 인용 주변에 대표·회사 측 발언이나 목표·계획·예정 표현이 있으면 인정하지 않는다)
- 상용 운영 문항은 인용 주변에 예정·목표·실증·PoC·시범 표현이 있거나 인용에 수량·고객이 없으면 인정하지 않는다
- D1(최근 라운드)은 LLM 답을 쓰지 않고 적격성 관문에서 인용 검증을 마친 값으로 코드가 판정한다
  (라운드 시점이 기준일 이후이거나 단계 인용이 예정·추진 중인 라운드면 완료된 투자가 아니므로 UNKNOWN)
v3 재검사 (LLM 호출·프롬프트는 그대로. 문항 플래그 4종은 YES 를 UNKNOWN 으로 내리는 쪽, 제품명 키는 근접 검사를 넓히는 쪽)
- track_record(F2): 인용에 학력·전공·학위 표현만 있고 특허·논문·연구·경력·직책 이력 표현이 없으면 UNKNOWN
  (이력 표현은 검증된 인용에서만 찾는다. LLM 이유문의 '경력이 확인된다'는 근거가 아님)
- no_plan(M3·M4·R4): 인용이 완료 표지 없는 예정·계획·방침·도입할·추진 문장이면 UNKNOWN (수익 모델·고객 경제성은 실제여야 함)
- numeric(M1·M2·M3·R1·R2·D3): need 가 수치를 요구하는 문항. 인용에 숫자(아라비아 숫자·'세 곳' 같은 수량 표현)가 없으면 UNKNOWN
- min_competitors(C2: 2): 실재가 확인된 경쟁사(company['confirmed_competitors'], 경쟁사 에이전트가 셈 — 같은 분야 적격 후보이거나
  회사명이 서로 다른 도메인 2곳 이상에서 확인된 회사)가 이 수보다 적으면 UNKNOWN (값이 없으면 0곳으로 본다)
- 회사명 근접 검사(±300자)에 회사 제품·서비스 이름도 회사 키로 인정 (기술 요약 에이전트의 tech.product 에서 이름만 뽑음)
- confidence(상·중·하): 같은 인용이 실린 제3자 출처 수로 정하는 표시용 값. 판정·배수에 쓰지 않는다
R1 공시 규칙 (DART, tools/dart.py): 후보에 DART 공시(감사보고서·사업보고서)의 연도가 적힌 매출액이 있고 그 공시가 최근 24개월 안에
접수됐으면, LLM 이 미확인(UNKNOWN)으로 둔 R1 을 코드가 YES 로 판정한다(근거 = 공시 근거 id, 인용 = 공시 근거 본문의 'OO YYYY년 매출액 N원').
YES·NO 판정은 바꾸지 않고, 공시가 없는 회사(DART 에 없음)는 아무것도 바뀌지 않는다 — DART 에 없다는 사실은 판정에 쓰지 않는다.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from functools import lru_cache
from typing import Literal, TypedDict

import yaml
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from core.config import ROOT, get_config
from core.llm_bounded import bounded
from core.prompts import render
from core.recency import DONE, future_round, only_future_events, planned_only
from rag.index import kiwi_tokenize
from rag.loader import load_manifest
from tools.grounding import fuzzy_in, norm
from tools.sources import SourceRegistry

DIMENSIONS = ("founder", "market", "product", "competition", "traction", "deal")
SIGNAL = {"YES": 1, "NO": -1, "UNKNOWN": 0, "N/A": None}   # Payne 배수에 쓰는 문항 신호 x

DB_SITES = {"THE VC", "혁신의숲"}
# 아래 표지들은 공백·기호를 뺀 소문자 글자(norm)에서 찾는다
# 제3자 근거 문항: 기사에 실렸어도 대표·회사 측 발언이나 목표·계획이면 제3자가 확인한 사실이 아니다
COMPANY_VOICE = ("대표는", "대표가", "대표이사는", "대표의설명", "라고말했다", "라고밝혔다", "설명했다", "설명이다",
                 "회사측", "회사는", "목표", "계획", "예정")
# 영어 발화 표지는 공백을 지우면 'has aided' 같은 오탐이 생겨 원문에서 단어 경계로 찾는다
COMPANY_VOICE_EN = re.compile(r"\b(said|says|plans to|aims to|according to the company)\b", re.I)
# 상용 운영 문항: 예정·실증 단계를 뜻하는 표현
PLANNED = re.compile(r"상용화를기점|상용화예정|출시예정|목표|실증|poc|proofofconcept|시범|(?<!auto)pilot")
# 상용 운영 문항 YES 요건(설치 수·면적·두수·고객명): 숫자, 한글 수량 표현, 농협·법인 같은 고객 이름
SCALE = re.compile(r"\d|농협|영농조합|농업회사법인")
SCALE_WORD = re.compile(r"(?<![가-힣])(한|두|세|네|다섯|여섯|일곱|여덟|아홉|수십|수백|수천)\s*(곳|개소|농가|농장|마리)")
# NO 판정 이유에 이런 말이 있으면 반대 사실이 아니라 근거 부족이다
NO_HEDGE = re.compile(r"추정|불분명|명확하지않|확인되지않|없어|없으므로|근거가없")
# YES 이유가 확인이 아니라 추정이면(예: "재계약을 추정할 수 있는") 근거가 YES 를 뒷받침하지 못한다 (공백 제거 후 검사)
YES_HEDGE = re.compile(r"추정|가능성|것으로보|짐작|추론|으로보인다|인듯|일수있")
# 인용문 자체가 부정을 말하면("특허 없음", "인증을 받지 않았다") 이유에 '없어'가 있어도 반대 사실로 본다
NEGATED = re.compile(r"없|않|아니|불가|미보유|미등록|\bno\b|\bnot\b|\bnever\b", re.I)
UNDISCLOSED = ("비공개", "미공개", "undisclosed", "비밀", "n/a")

# ── v3 재검사 표현 목록 (원문 인용에서 찾는다)
# track_record: 학력 표현과 이력 표현. '출신'은 학교·회사 모두에 쓰여 어느 쪽에도 넣지 않는다.
# '개발'·'CTO' 같은 현재 직함·일반 서술과 막연한 '경험'('어릴 때부터 로봇 제작 경험')은 이력으로 보지 않는다
# ('AI 로봇을 전공해 개발에 일가견' 은 학력뿐). 기간('30여년간'·'7년 동안')·前·이전 창업은 이력이다
EDUCATION = re.compile(r"전공|학위|학사|석사|박사|졸업|대학|학과|학부"
                       r"|\b(?:ph\.?\s?d|bachelor'?s?|master'?s?|basc|bsc|msc|degrees?|graduat\w*|major(?:ed)?|universit\w*)\b",
                       re.I)
TRACK_RECORD = re.compile(r"특허|발명|논문|저자|학회|연구|경력|근무|재직|역임|총괄|팀장|본부장|수석|선임|엔지니어|개발자|前"
                          r"|\d+\s*여?\s*년\s*(?:간|동안|이상)|(?:이전|첫)\s*창업|창업\s*(?:경험|이력)"
                          r"|\b(?:patents?|inventor|papers?|publications?|published|research\w*|lab(?:oratory)?|worked|"
                          r"working|former(?:ly)?|previously|engineers?|scientists?|led|veteran|years?\s+(?:at|of|in))\b",
                          re.I)
# no_plan: core.recency.planned_only 의 예정·계획·방침 표현에 '추진·도입할·검토 중' 같은 계획 서술을 더한다
PLAN_EXTRA = re.compile(r"추진\s*(?:중|하고|할|한다|하며|해|키로|하기로|을|를)|도입할|검토\s*중|준비\s*중|계획\s*중"
                        r"|(?:준비|검토|모색|계획|협의|논의)\s*하고\s*있|\bplann(?:ed|ing)\s+to\b", re.I)
# 계획 문장이라도 지금 하고 있는 일이 함께 적혀 있으면 완료 사실 ('요금을 받고 있으며 내년 해외로 넓힐 계획',
# '판매로 매출을 올리고 있으며 내년 구독형으로 전환할 계획'). 추진·준비·검토·앞두고 있다는 진행 중인 계획이라 뺀다
PLAN_DONE_EXTRA = re.compile(r"(?<!추진하)(?<!계획하)(?<!준비하)(?<!검토하)(?<!모색하)(?<!협의하)(?<!논의하)(?<!앞두)"
                             r"(?<!추진)(?<!준비)고\s*있(?:다|으며|고|습니다|는|어)")
# numeric: 숫자 또는 수량 표현 ('세 곳', '수백 농가', '절반', 'two farms')
NUMBER = re.compile(r"\d|절반|반값|(?<![가-힣])(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|수십|수백|수천|수만)\s*"
                    r"(?:곳|개|개소|농가|농장|마리|명|배|건|대|톤|가지|해|차례|만)"
                    r"|\b(?:two|three|four|five|six|seven|eight|nine|ten|dozens?|hundreds?|thousands?|half|halved|double[ds]?"
                    r"|triple[ds]?|twice)\b", re.I)
# 제품명 후보에서 뺄 업계 일반어 (일반어가 회사 키가 되면 업계 기사 문장도 '이 회사 이야기'로 통과한다)
GENERIC_PRODUCT_WORDS = {
    "ai", "iot", "ict", "gps", "rtk", "lidar", "saas", "app", "api", "robot", "robots", "robotics", "software",
    "platform", "solution", "solutions", "system", "systems", "service", "services", "device", "devices", "sensor",
    "sensors", "drone", "drones", "farm", "farms", "farming", "smart", "smartfarm", "data", "cloud", "autonomous",
    "agricultural", "agriculture", "agtech", "electric", "modular", "vision", "series", "technology", "technologies",
    "tech", "inc", "ltd", "corp", "the", "and", "for", "with",
    "솔루션", "서비스", "시스템", "플랫폼", "로봇", "디바이스", "기기", "장비", "센서", "드론", "소프트웨어", "하드웨어", "앱",
    "어플리케이션", "애플리케이션", "모듈", "키트", "스마트팜", "스마트", "농업", "농작업", "농산물", "농작물", "작물", "자율",
    "자율주행", "원격", "원격제어", "정밀", "정밀농업", "축산", "사료", "사료관리", "병해충", "생육", "온실", "도심", "도심농장",
    "수직농장", "실내", "노지", "위성", "인공위성", "데이터", "클라우드", "모바일", "무인", "전기", "통합", "지능형", "기반",
    "맞춤형", "다목적", "환경제어", "관제", "자동화", "예측", "진단", "모니터링", "수확", "방제", "재배", "농기계", "트랙터",
    "인공지능", "계측", "시리즈",
}
_QUOTED_NAME = re.compile(r"[‘'\"“「『]([^‘’'\"“”「」『』]{2,30})[’'\"”」』]")
_LATIN_NAME = re.compile(r"\b[A-Za-z][A-Za-z0-9\-]{2,}\b")
_CITE_ID = re.compile(r"\[[WD][0-9a-f]{4,}\]")
_PAREN = re.compile(r"\(([^()]*)\)")


class Row(TypedDict):
    """문항 1개 판정 결과."""
    dim: str
    qid: str
    short: str
    point: str
    question: str
    bessemer_q: list[int]
    answer: Literal["YES", "NO", "UNKNOWN", "N/A"]
    x: int | None                   # 신호: YES 1, NO -1, UNKNOWN 0, N/A None
    evidence_ids: list[str]
    quote: str
    rationale: str
    confidence: Literal["상", "중", "하"] | None   # 표시용 (판정·배수에 쓰지 않음). YES·NO 만, 그 밖은 None


class DimResult(TypedDict):
    """차원(Scorecard 기준) 1개 판정 결과. 분석 에이전트는 이 값을 state[키]['criterion'] 에 둔다."""
    dim: str
    name: str
    weight: int
    owner: str
    rows: list[Row]
    yes: int
    no: int
    unknown: int
    na: int
    n: int                          # 분모 = 문항 수 − N/A
    rejected_yes: list[dict]        # 코드가 UNKNOWN 으로 강등한 YES [{qid, reason, quote}]
    quote_retried: int              # 인용이 원문에 없어 다시 물은 문항 수


# 응답 스키마. LLM 캐시 키에 이 클래스의 모듈 경로가 들어가(response_format=<class 'core.judge.Answers'>)
# v1 캐시(agents.decision.Answers)와는 키가 다르다. v2 는 [분석 요약]이 달라 어차피 새로 판정한다.
class Answer(BaseModel):
    qid: str
    quote: str = Field(description="판정을 뒷받침하는 근거 원문 그대로의 짧은 인용 (80자 이내). 없으면 빈 문자열")
    evidence_ids: list[str] = Field(description="인용이 나온 근거 id")
    verdict: Literal["YES", "NO", "UNKNOWN", "N/A"]
    rationale: str = Field(description="한 문장 이유")


class Answers(BaseModel):
    answers: list[Answer]


class Verdict(BaseModel):
    """YES 검증(prompts/verify_yes.md): 인용이 문항의 인정 조건을 직접 충족하는가 (이진)."""
    supports: bool = Field(description="인용이 인정 조건을 직접 충족하는 구체적 사실이면 true, 아니거나 애매하면 false")
    reason: str = Field(description="한 문장 이유")


def _raw_context(quote: str, texts: list[str], window: int = 350) -> str:
    """검증자에게 보여 줄 인용 주변 원문(사람이 읽는 원래 글자). 위치를 못 찾으면 정규화 문맥으로 대신한다."""
    head = (quote or "").strip()[:20]
    for t in texts:
        pos = t.find(head) if head else -1
        if pos >= 0:
            return t[max(0, pos - window): pos + len(quote) + window]
    for t in texts:
        if (around := _around(quote, t, window)):
            return around
    return "(주변 원문을 찾지 못함)"


def _source_line(s: dict) -> str:
    if s.get("kind") == "doc":
        return f"공공·연구기관 문서 — {s.get('publisher') or ''} {s.get('year') or ''}".strip()
    return f"웹 — {s.get('site') or ''}, {s.get('date') or '게시일 미상'}"


def verify_mode() -> str:
    """decision.verify_yes: off(검증 안 함) · flag(검증자 이견을 표시만, 판정 유지 — 기본) · gate(이견이면 UNKNOWN).
    mini 등급 검증자의 표본 정확도가 50~60% 라(2026-10-12, 24건 대조) 판정을 바꾸는 데 쓰지 않고 실사 표시로 쓴다."""
    m = get_config().decision.get("verify_yes", "off")
    return {True: "gate", False: "off"}.get(m, m) if isinstance(m, bool) else str(m)


def _verify_yes(q: dict, company: str, quote: str, srcs: list[dict], texts: list[str], run_date: str) -> Verdict:
    return bounded(Verdict, "judge").invoke(render(
        "verify_yes", company=company, question=q["text"], need=q.get("need", ""), quote=quote, run_date=run_date,
        sources=" / ".join(_source_line(s) for s in srcs) or "-", context=_raw_context(quote, texts)))


_norm = norm


def load_rubric() -> dict:
    """rubric.yaml 전체 (dimensions·bessemer·gates·deal_killers·decision_rule)."""
    with open(ROOT / "rubric.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def company_keys(c: dict) -> list[str]:
    """회사 이름 표기들(영문명·공식명·통칭)을 정규화한 목록. 인용 주변 회사명 확인과 회사 도메인 판별에 쓴다.
    v1 decision_node 의 company_keys 계산과 같다(정규화 후 2글자 이상만)."""
    return [norm(x) for x in (c.get("name_en"), c.get("official_name"), c.get("name")) if x and len(norm(x)) >= 2]


def _name_ok(word: str, ckeys: list[str]) -> str | None:
    """제품명 후보 → 정규화한 키. 3글자 미만·일반어·꾸밈말(…형·…용·…적·…식)·회사명(일부 포함)이면 None."""
    w = norm(re.sub(r"(의|와|과)$", "", word.strip()))
    if len(w) < 3 or w in GENERIC_PRODUCT_WORDS or re.search(r"[형용적식]$", w):
        return None
    if any(k in w or w in k for k in ckeys):
        return None
    return w


def product_keys(c: dict) -> list[str]:
    """회사 제품·서비스 이름을 정규화한 목록. c['product'](기술 요약 에이전트 tech.product 문장)에서 이름 꼴만 뽑는다.
    - 따옴표 속 이름: "로봇 '옴니파머'" → 옴니파머
    - 대문자가 든 영문 단어: 'Amiga 모듈형 …' → amiga (회사 영문명 일부는 제외)
    - 회사명을 괄호로 단 이름: '데이팜 (트랙팜의 …)' → 데이팜
    - '이름 + 설명' 꼴 항목의 첫 단어: '마이피드 사료관리 서비스 및 …' → 마이피드 (첫 단어가 '회사의'면 이름 없음)
    3글자 미만·업계 일반어(GENERIC_PRODUCT_WORDS)·꾸밈말·회사명은 뺀다. product 가 없으면 []."""
    text = _CITE_ID.sub(" ", str(c.get("product") or ""))
    if not text.strip():
        return []
    ckeys = company_keys(c)
    found = [m.group(1) for m in _QUOTED_NAME.finditer(text)]
    found += [w for w in _LATIN_NAME.findall(text) if any(ch.isupper() for ch in w)]
    for m in _PAREN.finditer(text):   # '데이팜 (트랙팜의 …)': 괄호 안에 회사명이 있으면 괄호 바로 앞 단어가 제품명
        before = text[:m.start()].split()
        if before and any(k in norm(m.group(1)) for k in ckeys):
            found.append(before[-1])
    plain = _PAREN.sub(" ", _QUOTED_NAME.sub(" ", text))
    for item in re.split(r",|/|\s및\s|\band\b", plain):
        words = item.split()
        if len(words) >= 2 and not any(norm(re.sub(r"의$", "", words[0])) == k for k in ckeys):
            found.append(words[0])
    keys = [k for k in (_name_ok(w, ckeys) for w in found) if k]
    return list(dict.fromkeys(keys))


def judge_company(state: dict) -> dict:
    """판정에 넘길 회사 dict: state['current'] + 기술 요약의 제품 문장(state['tech']['product']). 제품명이 회사명 근접 검사의
    키가 된다. 기술 요약 노드는 자기 결과를 {**state, 'tech': out} 으로 넘긴다. 창업자 노드는 기술 요약 전이라 제품명이 없다.
    v3: 경쟁사 비교를 마쳤으면 실재가 확인된 경쟁사 수(state['competition']['confirmed_competitors'])도 붙인다
    (C2 min_competitors 검사용. 경쟁사 노드는 {**state, 'competition': out} 으로 넘긴다)."""
    product = (state.get("tech") or {}).get("product")
    c = {**state["current"], "product": product} if product else dict(state["current"])
    comp = state.get("competition") or {}
    if "confirmed_competitors" in comp:
        c["confirmed_competitors"] = comp["confirmed_competitors"]
    return c


def evidence_pool(state: dict, extra: list[str] = ()) -> list[str]:
    """판정에 쓸 근거 id 목록: current.evidence_ids + founder/tech/market/competition 의 pool_ids + extra.
    순서를 유지하고 중복을 없애며, 근거 저장소(registry)에 있는 id 만 남긴다.
    각 에이전트는 자기 차례까지 모인 근거로 판정한다(뒤 에이전트의 pool_ids 는 아직 비어 있음)."""
    reg = SourceRegistry(state.get("registry"))
    ids = list((state.get("current") or {}).get("evidence_ids", []))
    for key in ("founder", "tech", "market", "competition"):
        ids += (state.get(key) or {}).get("pool_ids", [])
    ids += list(extra)
    return [i for i in dict.fromkeys(ids) if reg.get(i)]


# ── 인용 검증 (v1 agents/decision.py 에서 그대로 옮김)
def _quote_sources(quote: str, cited: list[str], pool: list[str], reg: SourceRegistry) -> list[str]:
    """인용문이 실제로 들어 있는 근거 id. LLM 이 id 를 잘못 적어도 후보의 근거 전체에서 찾아 바로잡는다."""
    hit = [i for i in cited if fuzzy_in(quote, reg.text(i))]
    return hit or [i for i in pool if fuzzy_in(quote, reg.text(i))]


def _around(quote: str, text: str, window: int) -> str | None:
    """인용문과 그 앞뒤 window 글자(공백·기호를 뺀 글자 기준). 근거 본문에서 위치를 못 찾으면 None."""
    t, q = _norm(text), _norm(quote)
    pos = t.find(q[:12]) if len(q) >= 12 else t.find(q)
    if pos < 0:  # 인용이 조금 달라 위치를 못 찾으면 인용 앞부분 여러 조각으로 다시 찾는다
        pos = next((t.find(q[i:i + 10]) for i in range(0, max(1, len(q) - 10), 10) if t.find(q[i:i + 10]) >= 0), -1)
    if pos < 0:
        return None
    return t[max(0, pos - window): pos + len(q) + window]


def _near_company(quote: str, text: str, keys: list[str], window: int = 300) -> bool:
    """인용문이 나온 위치 앞뒤 window 글자 안에 회사명이 있는지 (문서 어딘가에 회사명만 있으면 되는 허점 차단)."""
    near = _around(quote, text, window)
    return near is not None and any(k in near for k in keys)


def _company_voice(quote: str, text: str, keys: list[str], window: int = 120) -> bool:
    """인용 앞뒤 window 글자 안에 대표·회사 측 발언이나 목표·계획 표지가 있으면 True (제3자 근거 아님).
    신문 기사라도 대표 인터뷰를 옮긴 문장이면 회사 자체 주장이다. 인용 위치를 못 찾으면 발화자를 확인할 수 없어 True."""
    near = _around(quote, text, window)
    if near is None:
        return True
    raw_pos = text.find(quote[:20])
    raw = text[max(0, raw_pos - window): raw_pos + len(quote) + window] if raw_pos >= 0 else ""
    return (any(v in near for v in COMPANY_VOICE) or any(k + "에따르면" in near for k in [*keys, "회사", "업체"])
            or bool(COMPANY_VOICE_EN.search(raw)))


def _planned(quote: str, texts: list[str], window: int = 120) -> bool:
    """상용 운영 문항: 인용이나 그 앞뒤 window 글자에 예정·목표·실증·PoC·시범 표현이 있으면 True."""
    zones = [_norm(quote)] + [z for t in texts if (z := _around(quote, t, window))]
    return any(PLANNED.search(z) for z in zones)


def _has_scale(quote: str, keys: list[str]) -> bool:
    """상용 운영 문항 YES 요건: 인용에 설치 수·면적·두수 같은 수량이나 고객 이름이 있는지 (회사 자신의 이름은 빼고 본다)."""
    q = _norm(quote)
    for k in keys:
        q = q.replace(k, "")
    return bool(SCALE.search(q) or SCALE_WORD.search(quote))


def _education_only(quote: str, rationale: str) -> bool:
    """track_record 문항: 인용·이유에 학력·전공·학위 표현이 있고, 인용에 특허·논문·연구·경력·직책 이력 표현이 없으면 True."""
    return bool(EDUCATION.search(f"{quote} {rationale}")) and not TRACK_RECORD.search(quote or "")


def _plan_only(quote: str, run_date: str) -> bool:
    """no_plan 문항: 인용이 완료 표지 없는 예정·계획·방침·추진 문장이면 True (core.recency.planned_only + 추진·도입할·검토 중)."""
    q = quote or ""
    if PLAN_DONE_EXTRA.search(q):
        return False
    return planned_only(q, run_date) or (bool(PLAN_EXTRA.search(q)) and not DONE.search(q))


def _site(s: dict) -> str:
    return str(s.get("site") or s.get("publisher") or s.get("url") or s.get("id") or "")


def _confidence(quote: str, ev: list[str], pool: list[str], reg: SourceRegistry, keys: list[str],
                search_pool: bool = True) -> str:
    """표시용 신뢰도: 같은 인용이 실린 근거 중 제3자 출처(회사 도메인이 아닌 웹·공공 문서)의 서로 다른 사이트·발행처 수 —
    2곳 이상 '상', 1곳 '중', 0곳(회사 홈페이지 등 회사 자체 자료뿐) '하'.
    기사 속 대표 발언 여부는 보지 않는다 (그 검사는 third_party 문항 판정이 따로 한다. 120자 안의 '회사는'만으로
    기자가 확인한 투자 사실까지 '하'가 되는 것을 막는다). search_pool False(D1)면 판정 근거 ev 만 본다."""
    srcs = list(ev)
    if search_pool and quote.strip():
        srcs = list(dict.fromkeys(srcs + [i for i in pool if i not in srcs and fuzzy_in(quote, reg.text(i))]))
    third = {_site(s) for i in srcs if (s := reg.get(i)) and _is_third_party(s, keys)}
    return "상" if len(third) >= 2 else "중" if third else "하"


def _passages(pool: list[str], reg: SourceRegistry, size: int = 520, step: int = 420) -> list[tuple[str, str]]:
    out = []
    for sid in pool:
        t = reg.text(sid)
        for k in range(0, max(1, len(t) - 80), step):
            out.append((sid, t[k:k + size]))
    return out


def _evidence_for(dim: dict, company: str, passages: list[tuple[str, str]], bm25, reg: SourceRegistry,
                  per_q: int = 6, cap: int = 24) -> str:
    """문항마다 관련 깊은 근거 문단을 따로 찾아 합친다 (후보가 모은 근거에 대한 작은 RAG).
    항목 전체를 한 번에 검색하면 특정 문항(예: 날짜가 있는 마일스톤)의 근거가 밀려나기 때문."""
    picked: list[int] = []
    for q in dim["questions"]:
        # 괄호 안 설명("예정·실증은 상용 운영 아님" 등)은 판정 규칙이지 검색어가 아니라서 뺀다 (넣으면 제외할 문장을 더 끌어옴)
        need = re.sub(r"\(.*?\)", "", q["need"])
        # 영문 근거(해외 후보·영문 기사)도 고르도록 문항의 영어 검색어(rubric text_en)를 함께 넣는다 — 모든 후보에 똑같이
        scores = bm25.get_scores(kiwi_tokenize(f"{company} {q['text']} {need} {q.get('text_en', '')}"))
        for i in sorted(range(len(passages)), key=lambda i: -scores[i])[:per_q]:
            if i not in picked:
                picked.append(i)
    lines = []
    for i in picked[:cap]:
        sid, text = passages[i]
        s = reg.get(sid)
        head = (f"{s['site']}, {s['date'] or '게시일 미상'}" if s["kind"] == "web"
                else f"{s['publisher']} {s['year']}, p.{s['page']}")
        lines.append(f"[{sid}] ({head}) {text}")
    return "\n\n".join(lines)


def _is_third_party(s: dict, company_keys: list[str]) -> bool:
    if s["kind"] == "doc":
        return True
    host = s["url"].split("/")[2].lower() if s["url"].count("/") >= 2 else ""
    return not any(k and k in _norm(host) for k in company_keys)


@lru_cache(maxsize=1)
def _doc_years() -> dict:
    """문서 코퍼스(data/manifest.yaml)의 doc_id → 발행 연도."""
    return {m["doc_id"]: m.get("year") for m in load_manifest()}


def _is_recent(s: dict, run_date: str) -> bool:
    if s["kind"] == "doc" or s.get("id", "").startswith("D"):  # 문서는 manifest 의 발행 연도가 기준 연도 − 2 ~ 기준 연도 (쪽마다 날짜가 없음)
        year = _doc_years().get(s.get("doc_id")) or s.get("year")
        try:
            return int(run_date[:4]) - 2 <= int(year) <= int(run_date[:4])
        except (TypeError, ValueError):
            return False
    if s["kind"] == "web" and s.get("site") in DB_SITES:
        return True  # 기업 DB 프로필은 현재 정보
    d = s.get("date") if s["kind"] == "web" else None
    if not d:
        return False
    try:
        days = (datetime.strptime(run_date, "%Y-%m-%d").date() - date.fromisoformat(d)).days
    except ValueError:
        return False
    return 0 <= days <= 730


def _events_too_old(text: str, run_date: str) -> bool:
    """문장 속 사건 날짜(2024년 6월, 2024-06, 2024.06)가 있고, 그 모두가 24개월보다 오래됐으면 True.
    LLM 은 날짜 계산을 자주 틀리므로 코드로 확인한다. 날짜가 없으면 판단하지 않는다(False)."""
    run = datetime.strptime(run_date, "%Y-%m-%d")
    months = [(int(y), int(m)) for y, m in re.findall(r"(20\d{2})\s*[년.\-/]\s*(\d{1,2})", text) if 1 <= int(m) <= 12]
    if not months:
        return False
    return all((run.year - y) * 12 + (run.month - m) > 24 for y, m in months)


def _round_rule(c: dict, run_date: str) -> tuple[str, list[str], str]:
    """D1: 최근 24개월 라운드의 단계·금액·시점. 적격성 검증 단계에서 원문 인용으로 확인된 값만 쓴다."""
    ids, rd, amount = c.get("stage_evidence_ids") or [], str(c.get("round_date") or ""), str(c.get("round_amount") or "")
    m = re.match(r"(20\d{2})(?:-(\d{1,2}))?", rd)
    if not ids or not m:
        return "UNKNOWN", [], "라운드 시점이 확인되지 않음 (코드 판정)"
    y, mo = int(m.group(1)), int(m.group(2) or 6)
    run = datetime.strptime(run_date, "%Y-%m-%d")
    months = (run.year - y) * 12 + (run.month - mo)
    if why := future_round(c, run_date):  # 기준일 이후·예정 라운드는 음수 개월이라 '24개월 안'으로 통과하던 것을 막는다
        return "UNKNOWN", [], f"{why} (코드 판정)"
    if months > 24:
        return "NO", ids, f"최근 라운드가 {rd} 로 24개월보다 오래됨 (코드 판정)"
    # 금액은 숫자가 있고 '비공개·미공개' 같은 표현이 없을 때만 확인된 것으로 본다 ('비공개' 문자열을 금액으로 세지 않게)
    if not re.search(r"\d", amount) or any(u in amount.lower() for u in UNDISCLOSED):
        return "UNKNOWN", [], f"{rd} {c.get('stage')} 라운드 금액 비공개{f' ({amount})' if amount else ''} (코드 판정)"
    return "YES", ids, f"{rd} {c.get('stage')} {amount} — 적격성 검증에서 원문 인용으로 확인 (코드 판정)"


def _dart_revenue(c: dict, pool: list[str], reg: SourceRegistry, run_date: str) -> tuple[list[str], str, str] | None:
    """R1 공시 규칙: c['dart']['revenue'](최신 공시 먼저) 중 근거 풀에 있고, 인용이 근거 본문에 있으며, 접수일이 최근 24개월인
    첫 매출액 → (근거 id 목록, 인용, 이유). 없으면 None."""
    for r in (c.get("dart") or {}).get("revenue") or []:
        sid, quote = r.get("evidence_id"), r.get("quote") or ""
        s = reg.get(sid) if sid else None
        if not s or sid not in pool or (r.get("amount") or 0) <= 0 or not fuzzy_in(quote, reg.text(sid)):
            continue
        if not _is_recent(s, run_date):
            continue
        return [sid], quote, (f"{r['year']}년 매출액 {r['amount']:,}원 — {r['report_nm']} 공시(금융감독원 전자공시시스템, "
                              f"{r.get('rcept_dt')} 접수)로 확인 (코드 판정, DART)")
    return None


def _qlines(questions: list[dict], note: dict | None = None) -> str:
    """프롬프트의 질문 목록 (v1 문구 그대로). note: {qid: 문항 끝에 붙일 재질문 안내}."""
    return "\n".join(f"- {q['id']}: {q['text']} (YES 요건: {q['need']})" + (note or {}).get(q["id"], "")
                     for q in questions)


def judge_dimension(dim_id: str, company: dict, pool_ids: list[str], reg: SourceRegistry, analysis: str,
                    run_date: str) -> DimResult:
    """차원 하나(dim_id: founder | market | product | competition | traction | deal)의 문항을 판정한다.

    - LLM(judge)은 문항별 판정(YES/NO/UNKNOWN/N/A)·근거 id·원문 인용·한 문장 이유만 답한다 (prompts/decision.md, v1 그대로).
      근거는 pool_ids 의 본문을 문단으로 잘라 문항마다 BM25 로 고른다.
    - 판정을 빠뜨린 문항은 한 번 더 묻고, 인용이 원문에 없는 YES·NO 도 한 번만 다시 묻는다.
    - 코드 강등: 인용이 본문에 없거나 회사명·제품명이 인용 ±300자 밖이면 UNKNOWN, 제3자·최근 24개월·문서 근거·상용 운영 요건 검사,
      v3 문항 플래그 검사(numeric 수치 · no_plan 계획 문장 · track_record 학력뿐 · min_competitors 확인된 경쟁사 수).
      NO 는 반대 문장 인용이 본문에 있고 회사 이야기이며, 이유가 근거 부족(추정·확인되지 않음 등)이 아닐 때만 남긴다.
    - company['product'](기술 요약의 제품 문장, judge_company 가 넣음)가 있으면 그 제품·서비스 이름도 근접 검사 키로 쓴다.
    - 행마다 confidence(상·중·하, 표시용)를 붙인다.
    - D1(최근 라운드)은 LLM 답을 쓰지 않고 적격성 관문에서 인용 검증을 마친 값으로 코드가 판정한다.
    - 근거 풀이 비어 있으면 LLM 을 부르지 않고 모든 문항을 UNKNOWN 으로 둔다(D1 은 코드 판정).
    반환: DimResult (rows 는 Row 목록, x 는 YES 1 / NO -1 / UNKNOWN 0 / N/A None).
    """
    rubric = load_rubric()
    d = next((x for x in rubric["dimensions"] if x["id"] == dim_id), None)
    if d is None:
        raise ValueError(f"알 수 없는 차원 {dim_id!r} (가능: {', '.join(DIMENSIONS)})")
    c = company
    pool = [i for i in dict.fromkeys(pool_ids) if reg.get(i)]
    keys = company_keys(c)
    near_keys = keys + product_keys(c)   # 회사명 근접 검사에만 제품·서비스 이름을 더한다 (제3자·발언 검사는 회사명 그대로)
    passages = _passages(pool, reg)

    got: dict[str, Answer] = {}
    rejected, retried = [], 0
    if passages:
        bm25 = BM25Okapi([kiwi_tokenize(p[1]) for p in passages])
        evidence = _evidence_for(d, c["official_name"], passages, bm25, reg)
        judge = bounded(Answers, "judge")

        def ask(questions: str) -> Answers:
            return judge.invoke(render("decision", dimension=d["name"], questions=questions, run_date=run_date,
                                       analysis=analysis, evidence=evidence))

        got = {a.qid: a for a in ask(_qlines(d["questions"])).answers}
        missing = [q for q in d["questions"] if q["id"] not in got]
        if missing:  # 판정을 빠뜨린 문항만 다시 묻는다
            got.update({a.qid: a for a in ask(_qlines(missing)).answers})
        # 인용이 [근거] 원문에 없는 YES·NO 는 한 번만 다시 묻는다 (분석 요약에서 베껴 온 인용 등). 그래도 없으면 아래에서 UNKNOWN
        bad = [q for q in d["questions"] if q["id"] != "D1" and (a := got.get(q["id"])) and a.verdict in ("YES", "NO")
               and a.quote.strip()
               and not _quote_sources(a.quote, [i for i in a.evidence_ids if i in pool], pool, reg)]
        if bad:
            in_summary = {q["id"] for q in bad if norm(got[q["id"]].quote) and norm(got[q["id"]].quote) in norm(analysis)}
            again = ask(_qlines(bad, {q["id"]: f" — 직전 인용 \"{got[q['id']].quote[:60]}\" 은 [근거] 원문에 없다"
                                               + (" ([분석 요약]의 문장이다 — 요약은 다른 에이전트가 한국어로 옮긴 것이라 인용할 수 없다. "
                                                  "같은 사실을 [근거] 원문(영문일 수 있음)에서 찾아라)" if q["id"] in in_summary else "")
                                               + ". [근거]에서 글자 그대로 다시 복사하거나, 없으면 UNKNOWN" for q in bad}))
            got.update({a.qid: a for a in again.answers if a.qid in {q["id"] for q in bad}})
            retried += len(bad)

    rows: list[Row] = []
    for q in d["questions"]:
        dissent = None
        a = got.get(q["id"])
        verdict = a.verdict if a else "UNKNOWN"
        ev = [i for i in (a.evidence_ids if a else []) if i in pool]
        note = a.rationale if a else ("판정 누락" if passages else "판정할 근거 없음 (근거 풀이 비어 있음)")
        quote = a.quote if a else ""
        if verdict == "N/A" and not q.get("na_allowed"):
            verdict, note = "UNKNOWN", "N/A 불가 문항 — " + note
        if q["id"] == "D1":  # 투자 라운드는 적격성 검증에서 인용 검증을 마친 값으로 코드가 판정 (LLM 판정은 쓰지 않음)
            verdict, ev, note = _round_rule(c, run_date)
            quote = c.get("stage_quote") or ""  # 적격성 검증에서 원문 확인을 마친 인용 (LLM 인용은 검증 전이라 쓰지 않음)
        elif verdict == "YES":
            fail = None
            dissent = None
            ev = _quote_sources(a.quote, ev, pool, reg)
            if not ev:
                fail = "인용문이 근거 본문에서 확인되지 않음"
            elif YES_HEDGE.search(re.sub(r"\s+", "", a.rationale)):
                fail = "판정 이유가 추정·가능성 표현 (확인된 사실이 아님)"
            elif not q.get("market_level") and not any(_near_company(a.quote, reg.text(i), near_keys) for i in ev):
                fail = "인용 주변(±300자)에 회사명이 없음 (제품·서비스 이름 포함. 업계 일반론이 아니라 이 회사 이야기여야 함)"
            elif q.get("doc_required") and not any(i.startswith("D") for i in ev):
                fail = "공공·연구기관 문서 근거 없음"
            elif q.get("third_party") and not any(_is_third_party(reg.get(i), keys) for i in ev):
                fail = "회사 자체 발표만 있음 (제3자 근거 필요)"
            elif q.get("third_party") and not any(_is_third_party(reg.get(i), keys)
                                                  and not _company_voice(a.quote, reg.text(i), keys)
                                                  for i in ev):
                fail = "회사 측 발언·계획(제3자 근거 아님)"
            elif q.get("commercial") and _planned(a.quote, [reg.text(i) for i in ev]):
                fail = "실증·예정 단계 (인용 주변에 예정·목표·실증·PoC·시범 표현)"
            elif q.get("commercial") and not _has_scale(a.quote, keys):
                fail = "상용 규모 근거 없음 (인용에 설치 수·면적·두수·고객명이 없음)"
            elif q.get("recent") and not any(_is_recent(reg.get(i), run_date) for i in ev):
                fail = "최근 24개월 이내 근거 아님"
            elif q.get("recent") and not q.get("market_level") and _events_too_old(f"{a.quote} {a.rationale}", run_date):
                fail = "언급된 사건 날짜가 모두 평가 기준일로부터 24개월보다 오래됨 (코드 날짜 검사)"
            elif q.get("recent") and not q.get("market_level") and only_future_events(a.quote, run_date):
                fail = "인용 속 사건 날짜가 평가 기준일 이후(예정)뿐 (코드 날짜 검사)"
            elif q.get("recent") and not q.get("market_level") and planned_only(a.quote, run_date):
                fail = "인용이 예정·계획·목표 문장 (완료된 사건 아님)"
            elif q.get("numeric") and not NUMBER.search(a.quote):
                fail = "수치 요구 문항인데 인용에 숫자가 없음 (코드 수치 검사)"
            elif q.get("no_plan") and _plan_only(a.quote, run_date):
                fail = "인용이 예정·계획·방침·추진 문장 (실제 수익 모델·효과가 아님)"
            elif q.get("track_record") and _education_only(a.quote, a.rationale):
                fail = "학력·전공만 있고 특허·논문·연구·경력 이력이 인용에 없음"
            if not fail and verify_mode() in ("flag", "gate"):
                # 코드 규칙을 통과한 YES 만 검증자 LLM 이 한 번 더 본다 (인용이 인정 조건을 직접 충족하는가, 애매하면 아님)
                v = _verify_yes(q, c.get("official_name") or c.get("name", ""), a.quote, [reg.get(i) for i in ev],
                                [reg.text(i) for i in ev], run_date)
                if not v.supports and verify_mode() == "gate":
                    fail = f"검증 단계: 인용이 문항의 인정 조건을 직접 충족하지 않음 — {v.reason}"
                elif not v.supports:
                    dissent = v.reason   # flag: 판정은 YES 그대로, 이견만 기록 → 투자 판단이 실사 항목으로 올린다
            elif q.get("min_competitors") and (n_rivals := int(c.get("confirmed_competitors") or 0)) < q["min_competitors"]:
                fail = (f"확인된 경쟁사 {n_rivals}곳 — {q['min_competitors']}곳 이상과 비교해야 함 "
                        "(같은 분야 적격 후보이거나 서로 다른 도메인 2곳 이상에서 확인된 경쟁사만 셈)")
            if fail:
                verdict, note = "UNKNOWN", f"{fail} → UNKNOWN 강등 ({note})"
                rejected.append({"qid": q["id"], "reason": fail, "quote": a.quote})
        elif verdict == "NO":  # "근거가 없다"는 NO 가 아니라 UNKNOWN. 반대 사실을 적은 문장이 원문에 있어야 NO
            ev = _quote_sources(a.quote, ev, pool, reg) if a.quote.strip() else []
            if not ev:
                verdict, note = "UNKNOWN", f"반대 근거 인용이 원문에서 확인되지 않음 → NO 대신 UNKNOWN ({note})"
            elif not q.get("market_level") and not any(_near_company(a.quote, reg.text(i), near_keys) for i in ev):
                # 업계 일반론(예: '농업용 로봇은 시범 운영 단계')은 이 회사에 대한 반대 사실이 아니다
                verdict, note = "UNKNOWN", f"반대 근거가 이 회사 이야기가 아님(인용 주변에 회사명 없음, 제품명 포함) → NO 대신 UNKNOWN ({note})"
            elif NO_HEDGE.search(re.sub(r"\s+", "", a.rationale)) and not NEGATED.search(a.quote):
                # 이유가 "추정·확인되지 않음·근거가 없어" 류이고 인용문 자체에 부정 표현도 없으면 근거 부족
                verdict, note = "UNKNOWN", f"반대 사실이 아니라 근거 부족 → NO 대신 UNKNOWN ({note})"
        if q["id"] == "R1" and verdict == "UNKNOWN" and (hit := _dart_revenue(c, pool, reg, run_date)):
            ev, quote, dart_note = hit  # 공시로 확인된 매출액 (LLM 이 미확인으로 둔 경우만)
            verdict, note = "YES", f"{dart_note} (LLM 판정: {note})"
        confidence = (_confidence(quote, ev, pool, reg, keys, search_pool=q["id"] != "D1")
                      if verdict in ("YES", "NO") else None)
        rows.append({"dim": d["id"], "qid": q["id"], "short": q.get("short", ""), "point": q.get("point", ""),
                     "question": q["text"], "bessemer_q": list(q.get("bessemer_q") or []), "answer": verdict,
                     "x": SIGNAL[verdict], "evidence_ids": ev if verdict in ("YES", "NO") else [],
                     "quote": quote if verdict in ("YES", "NO") else "", "rationale": note, "confidence": confidence,
                     "verifier": dissent if verdict == "YES" else None})

    count = {v: sum(r["answer"] == v for r in rows) for v in SIGNAL}
    return {"dim": d["id"], "name": d["name"], "weight": d["weight"], "owner": d["owner"], "rows": rows,
            "yes": count["YES"], "no": count["NO"], "unknown": count["UNKNOWN"], "na": count["N/A"],
            "n": len(rows) - count["N/A"], "rejected_yes": rejected, "quote_retried": retried}
