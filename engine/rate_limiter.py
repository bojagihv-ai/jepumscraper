"""
engine/rate_limiter.py — 적응형 요청 속도 제어
────────────────────────────────────────────────────────────────────────────
봇 탐지 우회 대상:
  • 요청 속도(Request Rate) : 너무 빠르면 봇 → 도메인별 토큰 버킷 + 지터
  • 버스트 패턴             : 짧은 시간에 집중 → 버스트 윈도우 감지·차단
  • 적응형 조절             : 429/503 → 자동으로 속도 반감

플랫폼별 기본 속도 (인간 평균 기준):
  쿠팡       : 2~4 req/min
  네이버쇼핑 : 3~5 req/min
  G마켓      : 2~4 req/min
"""

from __future__ import annotations

import logging
import random
import time
from threading import Lock
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── 플랫폼별 기본 요청 속도 설정 ────────────────────────────────────────────
# (min_delay, max_delay, burst_limit, burst_window_sec)
_PLATFORM_RATES: Dict[str, Tuple[float, float, int, float]] = {
    'coupang.com':        (12.0, 28.0, 3, 30),  # 12~28초 간격, 30초 내 최대 3개
    'shopping.naver.com': (8.0,  20.0, 4, 30),
    'naver.com':          (5.0,  15.0, 5, 30),
    'gmarket.co.kr':      (12.0, 25.0, 3, 30),
    '11st.co.kr':         (10.0, 22.0, 3, 30),
    'smartstore.naver.com': (8.0, 18.0, 4, 30),
    'default':            (8.0,  20.0, 4, 30),
}

# 429/503 발생 시 속도 감소 배율
_BACKOFF_MULTIPLIER = 2.5
_BACKOFF_RECOVERY   = 300  # 초 (5분 후 복구)


class _DomainBucket:
    """단일 도메인에 대한 토큰 버킷 + 버스트 제어."""

    def __init__(self, domain: str):
        cfg = None
        for key, val in _PLATFORM_RATES.items():
            if key in domain:
                cfg = val
                break
        cfg = cfg or _PLATFORM_RATES['default']

        self.min_delay, self.max_delay, self.burst_limit, self.burst_window = cfg
        self._last_request: float = 0.0
        self._request_times: list = []
        self._backoff_until: float = 0.0
        self._backoff_active: bool = False
        self._lock = Lock()

    def _effective_min_delay(self) -> float:
        if self._backoff_active and time.time() < self._backoff_until:
            return self.min_delay * _BACKOFF_MULTIPLIER
        elif self._backoff_active:
            self._backoff_active = False
            logger.debug("[RateLimiter] 백오프 해제")
        return self.min_delay

    def _jittered_delay(self) -> float:
        """지터가 적용된 대기 시간."""
        base = random.uniform(
            self._effective_min_delay(),
            self.max_delay * (1.5 if self._backoff_active else 1.0)
        )
        # ±20% 가우시안 지터
        jitter = random.gauss(0, base * 0.12)
        return max(self._effective_min_delay() * 0.5, base + jitter)

    def _in_burst_window(self) -> bool:
        """버스트 윈도우 내 요청이 너무 많은지 확인."""
        now = time.time()
        self._request_times = [t for t in self._request_times
                               if now - t < self.burst_window]
        return len(self._request_times) >= self.burst_limit

    def wait(self, url: str = ""):
        """요청 전 적절한 시간을 대기한다."""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_request
            needed  = self._jittered_delay()

            # 마지막 요청 이후 최소 대기
            if elapsed < needed:
                wait_sec = needed - elapsed
                logger.debug(f"[RateLimiter] 대기 {wait_sec:.1f}s ({url[:40]})")
                time.sleep(wait_sec)

            # 버스트 윈도우 초과 시 추가 대기
            extra_tries = 0
            while self._in_burst_window() and extra_tries < 5:
                extra = random.uniform(self.burst_window * 0.3, self.burst_window * 0.6)
                logger.debug(f"[RateLimiter] 버스트 제한 → 추가 대기 {extra:.1f}s")
                time.sleep(extra)
                extra_tries += 1

            self._last_request = time.time()
            self._request_times.append(self._last_request)

    def on_throttled(self, status_code: int):
        """429/503 응답 시 백오프 활성화."""
        if status_code in (429, 503, 520):
            self._backoff_active = True
            self._backoff_until  = time.time() + _BACKOFF_RECOVERY
            logger.warning(
                f"[RateLimiter] HTTP {status_code} → 백오프 {_BACKOFF_RECOVERY}s 활성화"
            )

    def on_success(self):
        """성공적인 응답 시 백오프 완화."""
        pass  # 복구는 시간 기반으로만 동작


class RateLimiter:
    """전체 도메인에 대한 적응형 속도 제한기."""

    def __init__(self):
        self._buckets: Dict[str, _DomainBucket] = {}
        self._lock = Lock()

    def _get_bucket(self, domain: str) -> _DomainBucket:
        with self._lock:
            if domain not in self._buckets:
                self._buckets[domain] = _DomainBucket(domain)
            return self._buckets[domain]

    def _extract_domain(self, url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).netloc

    def wait(self, url: str):
        """요청 전 속도 제한 대기."""
        domain = self._extract_domain(url)
        self._get_bucket(domain).wait(url)

    def on_response(self, url: str, status_code: int):
        """응답 후 상태 코드를 반영한다."""
        domain = self._extract_domain(url)
        bucket = self._get_bucket(domain)
        if status_code in (429, 503, 520):
            bucket.on_throttled(status_code)
        else:
            bucket.on_success()

    def reset(self, url: str):
        """도메인 버킷 초기화."""
        domain = self._extract_domain(url)
        with self._lock:
            self._buckets.pop(domain, None)


# ─── 전역 싱글톤 ─────────────────────────────────────────────────────────────

_LIMITER: Optional[RateLimiter] = None


def get_limiter() -> RateLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = RateLimiter()
    return _LIMITER


def wait(url: str):
    """간편 API: 요청 전 속도 제한 대기."""
    get_limiter().wait(url)


def on_response(url: str, status_code: int):
    """간편 API: 응답 후 상태 코드 반영."""
    get_limiter().on_response(url, status_code)
