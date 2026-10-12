"""LLM 토큰 사용량·비용 집계. LangSmith 없이도 실행마다 비용을 기록한다.

캐시(SQLite)에서 꺼낸 응답은 실제 API 호출이 아니므로 비용에 넣지 않는다(llm_output 이 없으면 캐시 응답).
"""
from __future__ import annotations

import threading

from langchain_core.callbacks import BaseCallbackHandler

from core.config import get_config


def model_price(model: str) -> tuple[float, float]:
    """모델 이름 → (입력, 출력) USD / 100만 토큰. 가격표는 config.yaml 의 models.price_per_mtok 에만 둔다.
    응답의 모델명에 날짜가 붙어도(…-2025-04-14) 가장 길게 일치하는 앞부분으로 찾고, 표에 없으면 생성 모델 가격으로 계산한다.
    프롬프트 캐시로 처리된 입력은 호출하는 쪽에서 입력 가격의 1/4 로 계산한다."""
    models = get_config().models
    table = models.get("price_per_mtok") or {}
    hit = max((k for k in table if model.startswith(k)), key=len, default=None)
    pi, po = table[hit] if hit else table[models.generator]
    return float(pi), float(po)


class BudgetExceeded(RuntimeError):
    """1회 실행의 LLM 비용이 상한(workflow.max_llm_usd)에 닿았다 — 새 호출을 막고 실행을 멈춘다."""


class CostTracker(BaseCallbackHandler):
    """비용 집계 + 비용 상한(교재 7장 ModelCallLimit·비용 초과 시 종료 정책과 같은 역할).
    상한에 닿으면 그다음 LLM 호출부터 시작 전에 BudgetExceeded 로 막는다(캐시 재생 실행은 비용 0이라 걸리지 않는다)."""
    raise_error = True   # 콜백에서 올린 예외(BudgetExceeded)가 LLM 호출 쪽으로 전달되게

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = self.cached = self.input_tokens = self.output_tokens = 0
        self.usd = 0.0

    def _check_budget(self) -> None:
        limit = float(get_config().workflow.get("max_llm_usd") or 0)
        if limit and self.usd >= limit:
            raise BudgetExceeded(f"LLM 비용 ${self.usd:.3f} 가 1회 실행 상한 ${limit:.2f}(workflow.max_llm_usd)에 닿아 "
                                 "새 호출을 멈춥니다")

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self._check_budget()

    def on_llm_start(self, serialized, prompts, **kwargs) -> None:
        self._check_budget()

    def on_llm_end(self, response, **kwargs) -> None:
        out = response.llm_output or {}
        usage = out.get("token_usage") or {}
        with self.lock:
            if not out:
                self.cached += 1
                return
            model = str(out.get("model_name", ""))
            pi, po = model_price(model)
            i, o = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            ci = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
            self.calls += 1
            self.input_tokens += i
            self.output_tokens += o
            self.usd += (i - ci) / 1e6 * pi + ci / 1e6 * pi / 4 + o / 1e6 * po

    def summary(self) -> dict:
        return {"api_calls": self.calls, "cache_hits": self.cached, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "usd": round(self.usd, 4)}


TRACKER = CostTracker()
