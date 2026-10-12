"""포털 전재본(다음·네이버 뉴스)의 원 매체 기사 주소 찾기.

왜: 2·3차로 옮겨 실린 자료 대신 원 소스를 확인해 인용한다 (2026-10-12 수업 피드백 — 레퍼런스 원 출처 확인).
포털 페이지에는 원문 주소가 없다(다음 v.daum.net 실측: 본문·메타 어디에도 원 매체 기사 URL 이 없음).
그래서 기사 제목과 원 매체 이름으로 한 번 검색해, 제목이 같은 원 매체 페이지를 찾는다.

- 검색은 tools/web_search._raw_search 를 그대로 써서 재현 캐시(replay/search)에 남는다 → 키 없이 같은 결과
- 못 찾으면 None: REFERENCE 는 지금처럼 '원 매체(포털 전재본)'으로 적고, 보고서 한계점에 건수를 밝힌다
"""
from __future__ import annotations

import re

from tools.sources import PORTALS, _origin, _site, full_title, is_portal

_TITLE_WORDS = re.compile(r"[\W_]+")
PORTAL_HOST = re.compile(r"(^|\.)(daum\.net|naver\.com|nate\.com|kakao\.com)$")   # 포털 회사의 다른 페이지도 원문이 아니다


def _key(title: str) -> str:
    return _TITLE_WORDS.sub("", title or "").lower()


def _same(a: str, b: str) -> bool:
    """제목이 같은 기사인지 (매체마다 붙는 ' - 조선비즈' 같은 꼬리·말줄임은 앞부분 일치로 허용)."""
    a, b = _key(a), _key(b)
    return bool(a) and bool(b) and (a == b or (min(len(a), len(b)) >= 15 and (a.startswith(b) or b.startswith(a))))


def resolve_origin(s: dict) -> dict | None:
    """포털 전재본 근거 s 의 원 매체 기사 {url, site}. 포털이 아니거나 못 찾으면 None.
    재현 실행에서 캐시에 없는 검색이면(오프라인) 찾지 않은 것으로 본다."""
    if not is_portal(s):
        return None
    title = full_title(s)
    outlet, _ = _origin(s)
    if not title:
        return None
    from tools.web_search import _raw_search

    try:
        got = _raw_search(f"{title} {outlet or ''}".strip(), "news", None, 5, None) or []
    except Exception:  # 오프라인 재현에서 캐시에 없는 검색, 공급자 오류 → 원문 미확인으로 둔다
        return None
    for r in got:
        url = r.get("url") or ""
        host = url.split("/")[2].lower() if url.count("/") >= 2 else ""
        if not host or host.endswith(PORTALS) or PORTAL_HOST.search(host) or not _same(r.get("title") or "", title):
            continue
        # 사이트 이름은 포털 본문에 적힌 원 매체(예: 조선비즈)를 우선한다 (도메인 표 SITE_NAMES 는 biz.chosun.com 도 '조선일보')
        return {"url": url.split("?daumarticle")[0], "site": outlet or _site(url, r.get("title") or "")}
    return None
