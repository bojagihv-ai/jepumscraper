"""
engine/smart_session.py - TLS 지문 위장 HTTP 세션
──────────────────────────────────────────────────────────────────────────────
실제 Chrome의 JA3/JA4 해시를 흉내내어 TLS 레벨 봇 탐지를 우회한다.

우선순위:
  1. tls-client  → Chrome 124 JA3/JA4 완벽 복제
  2. curl_cffi   → Chrome 임포소네이션 (TLS + HTTP/2)
  3. requests    → 기본 폴백 (TLS 위장 없음)

사용법:
    from engine.smart_session import SmartSession

    sess = SmartSession()
    resp = sess.get("https://www.coupang.com/np/search?q=노트북")
    print(resp.text[:500])
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ─── Chrome 124 실제 헤더 세트 ──────────────────────────────────────────────
_CHROME_124_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CHROME_124_HEADERS: Dict[str, str] = {
    "User-Agent": _CHROME_124_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8,"
              "application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "DNT": "1",
    "Cache-Control": "max-age=0",
}

# ─── tls-client 식별자 (Chrome 버전 × OS 매트릭스) ─────────────────────────
_TLS_CLIENT_IDENTIFIERS = [
    "chrome_124",
    "chrome_123",
    "chrome_122",
    "chrome_120",
]

# ─── 지수 백오프 재시도 설정 ────────────────────────────────────────────────
_RETRY_STATUS = {429, 503, 502, 520, 521, 522, 524}
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0     # 초
_BACKOFF_MAX = 60.0     # 초


class _FallbackResponse:
    """requests / curl_cffi / tls-client 응답을 통일된 인터페이스로 감싼다."""

    def __init__(self, raw):
        self._raw = raw

    @property
    def status_code(self) -> int:
        return getattr(self._raw, "status_code", 0)

    @property
    def text(self) -> str:
        try:
            return self._raw.text
        except Exception:
            return ""

    @property
    def content(self) -> bytes:
        try:
            return self._raw.content
        except Exception:
            return b""

    @property
    def headers(self) -> Dict[str, str]:
        try:
            return dict(self._raw.headers)
        except Exception:
            return {}

    @property
    def cookies(self):
        return getattr(self._raw, "cookies", {})

    def json(self) -> Any:
        return self._raw.json()

    def raise_for_status(self):
        if hasattr(self._raw, "raise_for_status"):
            self._raw.raise_for_status()


class SmartSession:
    """
    TLS 지문 위장 HTTP 세션.

    Parameters
    ----------
    proxy : str | None
        SOCKS5/HTTP 프록시 주소 (예: "socks5://user:pass@host:port")
    timeout : int
        요청 타임아웃 (초)
    cookies : dict | None
        초기 쿠키 딕셔너리
    tls_identifier : str | None
        tls-client 식별자 (None이면 랜덤 Chrome 124/123 선택)
    """

    def __init__(
        self,
        proxy: Optional[str] = None,
        timeout: int = 30,
        cookies: Optional[Dict[str, str]] = None,
        tls_identifier: Optional[str] = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self._cookies: Dict[str, str] = dict(cookies or {})
        self._tls_id = tls_identifier or random.choice(_TLS_CLIENT_IDENTIFIERS)
        self._backend: str = "none"
        self._session = self._build_session()

    # ──────────────────────────────────────────────────────────────────────
    # 세션 생성
    # ──────────────────────────────────────────────────────────────────────

    def _build_session(self):
        """백엔드 우선순위: tls-client → curl_cffi → requests"""
        # 1) tls-client
        try:
            import tlsclient.session as tls_lib  # noqa: F401
            return self._build_tls_client()
        except ImportError:
            pass

        try:
            import tls_client as tls_lib  # noqa: F401
            return self._build_tls_client()
        except ImportError:
            pass

        # 2) curl_cffi
        try:
            from curl_cffi import requests as cffi_req
            sess = cffi_req.Session(impersonate="chrome124")
            sess.headers.update(_CHROME_124_HEADERS)
            if self.proxy:
                sess.proxies = {"http": self.proxy, "https": self.proxy}
            self._backend = "curl_cffi"
            logger.info("[SmartSession] 백엔드: curl_cffi (Chrome124 임포소네이션)")
            return sess
        except ImportError:
            pass

        # 3) requests (폴백)
        import requests as req_lib
        sess = req_lib.Session()
        sess.headers.update(_CHROME_124_HEADERS)
        if self.proxy:
            sess.proxies = {"http": self.proxy, "https": self.proxy}
        self._backend = "requests"
        logger.warning("[SmartSession] 백엔드: requests (TLS 위장 없음)")
        return sess

    def _build_tls_client(self):
        """tls-client 세션 생성 (JA3/JA4 완벽 복제)."""
        try:
            import tls_client
            sess = tls_client.Session(
                client_identifier=self._tls_id,
                random_tls_extension_order=True,
            )
        except Exception:
            # 구버전 API 폴백
            import tls_client
            sess = tls_client.Session(client_identifier=self._tls_id)

        sess.headers.update(_CHROME_124_HEADERS)

        if self.proxy:
            sess.proxies = {"http": self.proxy, "https": self.proxy}

        self._backend = "tls-client"
        logger.info(f"[SmartSession] 백엔드: tls-client ({self._tls_id})")
        return sess

    # ──────────────────────────────────────────────────────────────────────
    # 쿠키 관리
    # ──────────────────────────────────────────────────────────────────────

    def update_cookies(self, cookies: Dict[str, str]):
        """쿠키를 세션에 병합한다."""
        self._cookies.update(cookies)
        try:
            # requests / curl_cffi style
            self._session.cookies.update(cookies)
        except Exception:
            pass

    def get_cookies(self) -> Dict[str, str]:
        """현재 쿠키 딕셔너리 반환."""
        merged = dict(self._cookies)
        try:
            merged.update(dict(self._session.cookies))
        except Exception:
            pass
        return merged

    def inject_bypass_cookies(self, bypass_cookies: Dict[str, str], url: str = ""):
        """bypass_engine.get_bypass_cookies() 결과를 세션에 주입한다."""
        if not bypass_cookies:
            return
        domain = ""
        if url:
            parsed = urlparse(url)
            domain = parsed.netloc.lstrip("www.")
        self.update_cookies(bypass_cookies)
        logger.debug(f"[SmartSession] 우회 쿠키 주입: {list(bypass_cookies.keys())} (도메인: {domain})")

    # ──────────────────────────────────────────────────────────────────────
    # HTTP 메서드
    # ──────────────────────────────────────────────────────────────────────

    def _do_request(self, method: str, url: str, **kwargs) -> _FallbackResponse:
        """실제 요청 + 지수 백오프 재시도."""
        kwargs.setdefault("timeout", self.timeout)

        # 현재 쿠키 병합
        if self._cookies:
            existing = kwargs.get("cookies", {})
            merged = {**self._cookies, **existing}
            kwargs["cookies"] = merged

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                if self._backend == "curl_cffi":
                    raw = getattr(self._session, method.lower())(url, **kwargs)
                elif self._backend == "tls-client":
                    raw = getattr(self._session, method.lower())(url, **kwargs)
                else:
                    raw = getattr(self._session, method.lower())(url, **kwargs)

                resp = _FallbackResponse(raw)

                # 429/503 → 재시도
                if resp.status_code in _RETRY_STATUS:
                    wait = min(_BACKOFF_BASE ** (attempt + 1), _BACKOFF_MAX)
                    wait += random.uniform(0, wait * 0.3)
                    logger.warning(
                        f"[SmartSession] {resp.status_code} → {wait:.1f}s 후 재시도 "
                        f"({attempt + 1}/{_MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue

                # 응답 쿠키 자동 수집
                try:
                    self._cookies.update(dict(raw.cookies))
                except Exception:
                    pass

                return resp

            except Exception as exc:
                last_exc = exc
                wait = min(_BACKOFF_BASE ** (attempt + 1), _BACKOFF_MAX)
                wait += random.uniform(0, wait * 0.2)
                logger.warning(
                    f"[SmartSession] 요청 예외 → {wait:.1f}s 후 재시도 "
                    f"({attempt + 1}/{_MAX_RETRIES}): {exc}"
                )
                time.sleep(wait)

        raise RuntimeError(f"[SmartSession] {_MAX_RETRIES}회 재시도 실패: {last_exc}")

    def get(self, url: str, **kwargs) -> _FallbackResponse:
        return self._do_request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> _FallbackResponse:
        return self._do_request("POST", url, **kwargs)

    def head(self, url: str, **kwargs) -> _FallbackResponse:
        return self._do_request("HEAD", url, **kwargs)

    # ──────────────────────────────────────────────────────────────────────
    # 편의 기능
    # ──────────────────────────────────────────────────────────────────────

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ─── 전역 세션 풀 (도메인별 재사용) ──────────────────────────────────────────
_SESSION_POOL: Dict[str, SmartSession] = {}


def get_domain_session(
    url: str,
    proxy: Optional[str] = None,
    bypass_cookies: Optional[Dict[str, str]] = None,
) -> SmartSession:
    """
    도메인별 SmartSession을 반환한다 (없으면 생성).
    bypass_engine 결과 쿠키를 자동으로 주입한다.
    """
    parsed = urlparse(url)
    domain = parsed.netloc
    key = f"{domain}:{proxy or 'direct'}"

    if key not in _SESSION_POOL:
        _SESSION_POOL[key] = SmartSession(proxy=proxy)
        logger.debug(f"[SmartSession] 새 세션 생성: {domain}")

    sess = _SESSION_POOL[key]

    if bypass_cookies:
        sess.inject_bypass_cookies(bypass_cookies, url)

    return sess


def clear_session_pool():
    """모든 도메인 세션을 닫고 풀을 초기화한다."""
    for sess in _SESSION_POOL.values():
        sess.close()
    _SESSION_POOL.clear()
