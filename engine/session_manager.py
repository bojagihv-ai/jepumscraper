"""
engine/session_manager.py — 세션 나이 + 쿠키 이력 관리
────────────────────────────────────────────────────────────────────────────
봇 탐지 우회 대상:
  • 세션 나이(Session Age)   : 갓 만든 세션은 의심 → 홈 방문으로 예열
  • 쿠키 이력(Cookie History): 첫 방문 쿠키 패턴과 재방문 패턴을 구분
  • 쿠키 지속성              : 동일 도메인에 대한 쿠키를 디스크에 영속화

핵심 개념:
  BrowserSession: 쿠키 jar + 방문 이력 + 나이를 갖는 단일 "브라우저 프로필"
  SessionPool   : 도메인별 예열된 세션 풀 (최소 MIN_WARM_SESSIONS개 유지)
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ─── 설정 ────────────────────────────────────────────────────────────────────
MIN_WARM_AGE    = 120      # 세션이 "신뢰받기" 위한 최소 나이 (초)
MAX_SESSION_AGE = 3600 * 4 # 4시간 이후 세션 폐기
SESSION_REUSE_LIMIT = 80   # 세션 하나당 최대 요청 수
MIN_WARM_SESSIONS = 2      # 도메인별 유지할 예열 세션 최소 수

try:
    import config
    _CACHE_DIR = Path(config.BASE_DIR) / 'data' / 'sessions'
except Exception:
    _CACHE_DIR = Path(__file__).parent.parent / 'data' / 'sessions'

_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ─── 데이터 클래스 ────────────────────────────────────────────────────────────

@dataclass
class BrowserSession:
    """단일 브라우저 세션 (하나의 가상 유저)."""

    session_id: str
    domain: str
    created_at: float          = field(default_factory=time.time)
    last_used:  float          = field(default_factory=time.time)
    request_count: int         = 0
    visited_urls: List[str]    = field(default_factory=list)
    cookies: Dict[str, str]    = field(default_factory=dict)
    headers: Dict[str, str]    = field(default_factory=dict)
    is_warm: bool              = False   # 홈 방문 완료 여부

    # ── 나이 관련 ──────────────────────────────────────────────
    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_used

    @property
    def is_expired(self) -> bool:
        return (
            self.age_seconds > MAX_SESSION_AGE
            or self.request_count >= SESSION_REUSE_LIMIT
        )

    @property
    def is_trusted(self) -> bool:
        """세션이 충분히 "나이 들어" 신뢰 가능한 상태인지."""
        return self.is_warm and self.age_seconds >= MIN_WARM_AGE

    # ── 쿠키 관리 ─────────────────────────────────────────────
    def update_cookies(self, new_cookies: Dict[str, str]):
        self.cookies.update(new_cookies)

    def add_visit(self, url: str):
        self.visited_urls.append(url)
        if len(self.visited_urls) > 50:
            self.visited_urls = self.visited_urls[-50:]
        self.last_used = time.time()
        self.request_count += 1

    # ── 직렬화 ────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            'session_id':    self.session_id,
            'domain':        self.domain,
            'created_at':    self.created_at,
            'last_used':     self.last_used,
            'request_count': self.request_count,
            'visited_urls':  self.visited_urls[-10:],
            'cookies':       self.cookies,
            'headers':       self.headers,
            'is_warm':       self.is_warm,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'BrowserSession':
        s = cls(session_id=d['session_id'], domain=d['domain'])
        s.created_at    = d.get('created_at',    time.time())
        s.last_used     = d.get('last_used',     time.time())
        s.request_count = d.get('request_count', 0)
        s.visited_urls  = d.get('visited_urls',  [])
        s.cookies       = d.get('cookies',       {})
        s.headers       = d.get('headers',       {})
        s.is_warm       = d.get('is_warm',       False)
        return s


# ─── 세션 풀 ─────────────────────────────────────────────────────────────────

class SessionPool:
    """
    도메인별 브라우저 세션 풀.

    사용법:
        pool = SessionPool()
        sess = pool.get('coupang.com')
        sess.update_cookies({'_abck': '...'})
        pool.release(sess)
    """

    def __init__(self):
        self._pools: Dict[str, List[BrowserSession]] = {}
        self._lock  = Lock()
        self._load_persisted()

    # ── 외부 API ──────────────────────────────────────────────

    def get(self, domain: str, require_warm: bool = True) -> BrowserSession:
        """
        가장 적합한 세션을 반환한다.
        require_warm=True이면 예열 세션만, 없으면 새 세션을 만들어 반환.
        """
        with self._lock:
            self._evict_expired(domain)
            pool = self._pools.setdefault(domain, [])

            candidates = (
                [s for s in pool if s.is_trusted]
                if require_warm
                else [s for s in pool if not s.is_expired]
            )

            if candidates:
                # 나이가 가장 어린 신뢰 세션 (덜 의심스러움)
                sess = min(candidates, key=lambda s: s.request_count)
                logger.debug(
                    f"[SessionPool] 기존 세션 재사용: {sess.session_id[:8]} "
                    f"나이={sess.age_seconds:.0f}s 요청={sess.request_count}"
                )
                return sess

            # 새 세션 생성
            sess = self._create_new(domain)
            pool.append(sess)
            logger.debug(f"[SessionPool] 새 세션 생성: {sess.session_id[:8]}")
            return sess

    def release(self, session: BrowserSession):
        """세션 반환 + 디스크 저장."""
        session.last_used = time.time()
        self._persist(session)

    def mark_warm(self, session: BrowserSession, cookies: Dict[str, str] = None):
        """홈 방문 완료 → 세션을 예열 상태로 표시."""
        session.is_warm = True
        if cookies:
            session.update_cookies(cookies)
        self._persist(session)
        logger.debug(f"[SessionPool] 세션 예열 완료: {session.session_id[:8]}")

    def inject_cookies(self, session: BrowserSession, cookies: Dict[str, str]):
        """외부(bypass_engine 등)에서 획득한 쿠키를 세션에 병합."""
        session.update_cookies(cookies)

    def count(self, domain: str) -> int:
        with self._lock:
            return len([s for s in self._pools.get(domain, []) if not s.is_expired])

    def warm_count(self, domain: str) -> int:
        with self._lock:
            return len([
                s for s in self._pools.get(domain, [])
                if s.is_trusted and not s.is_expired
            ])

    # ── 내부 유틸 ─────────────────────────────────────────────

    def _create_new(self, domain: str) -> BrowserSession:
        sid = f"{domain}_{int(time.time())}_{random.randint(1000, 9999)}"
        return BrowserSession(session_id=sid, domain=domain)

    def _evict_expired(self, domain: str):
        pool = self._pools.get(domain, [])
        before = len(pool)
        self._pools[domain] = [s for s in pool if not s.is_expired]
        removed = before - len(self._pools[domain])
        if removed:
            logger.debug(f"[SessionPool] {domain} 만료 세션 {removed}개 제거")

    # ── 영속화 ────────────────────────────────────────────────

    def _persist(self, session: BrowserSession):
        path = _CACHE_DIR / f"{session.session_id}.json"
        try:
            path.write_text(json.dumps(session.to_dict(), ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            logger.debug(f"[SessionPool] 저장 실패: {e}")

    def _load_persisted(self):
        """디스크에서 이전 세션들을 복원한다."""
        count = 0
        for p in _CACHE_DIR.glob('*.json'):
            try:
                data = json.loads(p.read_text(encoding='utf-8'))
                sess = BrowserSession.from_dict(data)
                if not sess.is_expired:
                    self._pools.setdefault(sess.domain, []).append(sess)
                    count += 1
                else:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
        if count:
            logger.info(f"[SessionPool] 이전 세션 {count}개 복원")


# ─── 쿠키 이력 빌더 ──────────────────────────────────────────────────────────

class CookieHistoryBuilder:
    """
    현실적인 쿠키 이력을 구성한다.
    실제 브라우저처럼 첫 방문 → 재방문 쿠키 패턴을 흉내낸다.
    """

    # 플랫폼별 기본 쿠키 (도메인 최초 방문 시 설정될 법한 값들)
    _FIRST_VISIT_COOKIES: Dict[str, Dict[str, str]] = {
        'coupang.com': {
            'sid': '',               # 세션 ID (빈 값→서버가 채움)
            'PCID': '',
            'MARKETID': 'WING',
            'overCountryCd': 'KR',
            'countryCode': 'KR',
        },
        'naver.com': {
            'NNB': '',
            'ASID': '',
            'nid_inf': '',
        },
        'shopping.naver.com': {
            'NNB': '',
            'ASID': '',
        },
    }

    @classmethod
    def build_first_visit(cls, domain: str) -> Dict[str, str]:
        """도메인 최초 방문 쿠키 세트 (아직 추적 쿠키 없음)."""
        base = {}
        for key, defaults in cls._FIRST_VISIT_COOKIES.items():
            if key in domain:
                base.update(defaults)
                break
        return base

    @classmethod
    def build_return_visit(cls, domain: str, session: BrowserSession) -> Dict[str, str]:
        """재방문 쿠키 세트 (기존 쿠키 + 세션 나이 반영)."""
        # 기존 쿠키를 기반으로 하되, 세션 나이를 반영한 타임스탬프 등 추가
        merged = dict(cls.build_first_visit(domain))
        merged.update(session.cookies)
        return merged

    @classmethod
    def get_cookies_for_request(cls, session: BrowserSession, url: str) -> Dict[str, str]:
        """현재 요청에 사용할 쿠키를 반환한다."""
        domain = urlparse(url).netloc.lstrip('www.')
        if not session.visited_urls:
            # 첫 요청 — 초기 쿠키
            return cls.build_first_visit(domain)
        return cls.build_return_visit(domain, session)


# ─── 전역 싱글톤 ─────────────────────────────────────────────────────────────

_POOL: Optional[SessionPool] = None


def get_pool() -> SessionPool:
    global _POOL
    if _POOL is None:
        _POOL = SessionPool()
    return _POOL
