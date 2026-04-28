"""
engine/navigation.py — 플랫폼별 현실적 탐색 경로 시뮬레이션
────────────────────────────────────────────────────────────────────────────
봇 탐지 우회 대상:
  • 상품/검색 경로(Navigation Path): 직접 URL 타격은 봇 신호
    → 홈 → 카테고리 → 검색 → 상품 순서로 탐색
  • Referer 체인               : 각 단계의 Referrer를 올바르게 설정
  • 세션 온도(Session Warmth)   : 홈 방문 + 카테고리 탐색으로 세션을 "익힘"

플랫폼 지원:
  coupang, naver, naver_shopping, gmarket, 11st
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─── 탐색 단계 정의 ──────────────────────────────────────────────────────────

@dataclass
class NavStep:
    url: str
    wait_min: float = 2.0
    wait_max: float = 5.0
    scroll_count: int = 3
    purpose: str = "browse"         # 'home'|'category'|'search'|'product'


# ─── 플랫폼 프로필 ────────────────────────────────────────────────────────────

_COUPANG_WARMUP: List[NavStep] = [
    NavStep("https://www.coupang.com",
            wait_min=2.5, wait_max=4.5, scroll_count=4, purpose="home"),
]

_COUPANG_CATEGORIES = [
    "https://www.coupang.com/np/categories/194085",   # 가전디지털
    "https://www.coupang.com/np/categories/393760",   # 생활용품
    "https://www.coupang.com/np/categories/115573",   # 패션
    "https://www.coupang.com/np/categories/194106",   # 식품
    "https://www.coupang.com/np/categories/115761",   # 스포츠
]

_NAVER_WARMUP: List[NavStep] = [
    NavStep("https://www.naver.com",
            wait_min=2.0, wait_max=4.0, scroll_count=2, purpose="home"),
    NavStep("https://shopping.naver.com",
            wait_min=1.5, wait_max=3.5, scroll_count=3, purpose="category"),
]

_NAVER_CATEGORIES = [
    "https://shopping.naver.com/home/p/index.naver?catId=50000803",   # 가전
    "https://shopping.naver.com/home/p/index.naver?catId=50000167",   # 패션의류
    "https://shopping.naver.com/home/p/index.naver?catId=50000165",   # 식품
]

_GMARKET_WARMUP: List[NavStep] = [
    NavStep("https://www.gmarket.co.kr",
            wait_min=2.0, wait_max=4.0, scroll_count=3, purpose="home"),
]

_11ST_WARMUP: List[NavStep] = [
    NavStep("https://www.11st.co.kr",
            wait_min=2.0, wait_max=4.0, scroll_count=3, purpose="home"),
]


class NavigationProfile:
    """
    플랫폼별 탐색 프로필.
    실제 사용자처럼 홈 → 카테고리 → 검색 → 상품 순서로 이동한다.
    """

    _PROFILES: Dict[str, dict] = {
        'coupang.com': {
            'warmup': _COUPANG_WARMUP,
            'categories': _COUPANG_CATEGORIES,
            'home': 'https://www.coupang.com',
            'search_tmpl': 'https://www.coupang.com/np/search?q={query}&channel=user&sorter=bestAsc',
            'referrer_base': 'https://www.coupang.com',
        },
        'naver.com': {
            'warmup': _NAVER_WARMUP,
            'categories': _NAVER_CATEGORIES,
            'home': 'https://www.naver.com',
            'search_tmpl': 'https://search.shopping.naver.com/search/all?query={query}&sort=rel',
            'referrer_base': 'https://shopping.naver.com',
        },
        'shopping.naver.com': {
            'warmup': _NAVER_WARMUP,
            'categories': _NAVER_CATEGORIES,
            'home': 'https://shopping.naver.com',
            'search_tmpl': 'https://search.shopping.naver.com/search/all?query={query}&sort=rel',
            'referrer_base': 'https://shopping.naver.com',
        },
        'gmarket.co.kr': {
            'warmup': _GMARKET_WARMUP,
            'categories': [],
            'home': 'https://www.gmarket.co.kr',
            'search_tmpl': 'https://browse.gmarket.co.kr/search?keyword={query}',
            'referrer_base': 'https://www.gmarket.co.kr',
        },
        '11st.co.kr': {
            'warmup': _11ST_WARMUP,
            'categories': [],
            'home': 'https://www.11st.co.kr',
            'search_tmpl': 'https://search.11st.co.kr/Search.tmall?kwd={query}',
            'referrer_base': 'https://www.11st.co.kr',
        },
    }

    @classmethod
    def get(cls, url: str) -> dict:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.replace('www.', '').replace('search.', '')
        for key, profile in cls._PROFILES.items():
            if key in domain or domain in key:
                return profile
        return {
            'warmup': [],
            'categories': [],
            'home': '',
            'search_tmpl': '{url}',
            'referrer_base': '',
        }

    @classmethod
    def get_warmup_steps(cls, url: str) -> List[NavStep]:
        return cls.get(url)['warmup']

    @classmethod
    def get_referrer(cls, target_url: str, previous_url: str = '') -> str:
        """다음 요청에 사용할 적절한 Referrer를 반환한다."""
        if previous_url:
            return previous_url
        profile = cls.get(target_url)
        return profile.get('referrer_base', '')

    @classmethod
    def get_random_category_url(cls, url: str) -> Optional[str]:
        profile = cls.get(url)
        cats = profile.get('categories', [])
        return random.choice(cats) if cats else None


class PlaywrightNavigator:
    """
    Playwright 페이지에서 현실적인 탐색 경로를 실행한다.
    sync Playwright (sync_playwright) 기반.
    """

    def __init__(
        self,
        page,
        on_scroll: Optional[Callable] = None,
        verbose: bool = False,
    ):
        self.page = page
        self._on_scroll = on_scroll
        self._verbose = verbose
        self._prev_url = ""

    # ── 공개 API ──────────────────────────────────────────────

    def warmup(self, target_url: str) -> Dict[str, str]:
        """
        target_url에 진입하기 전 예열 단계를 실행한다.
        반환: 획득한 쿠키 딕셔너리
        """
        steps = NavigationProfile.get_warmup_steps(target_url)
        all_cookies: Dict[str, str] = {}

        for step in steps:
            self._goto_step(step)
            ck = self._harvest_cookies()
            all_cookies.update(ck)

        # 20% 확률로 카테고리 한 곳 추가 방문 (더 자연스럽게)
        if random.random() < 0.20:
            cat_url = NavigationProfile.get_random_category_url(target_url)
            if cat_url:
                self._goto_step(NavStep(cat_url, 1.5, 3.0, 2, "category"))
                all_cookies.update(self._harvest_cookies())

        return all_cookies

    def navigate_to(self, url: str, wait_after: float = 3.0) -> bool:
        """지정 URL로 이동하고 인간다운 대기를 수행한다."""
        referer = NavigationProfile.get_referrer(url, self._prev_url)
        try:
            if referer:
                self.page.set_extra_http_headers({"Referer": referer})
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self._prev_url = url
        except Exception as e:
            logger.warning(f"[Navigator] goto 실패 {url[:60]}: {e}")
            return False

        # 인간다운 대기 + 스크롤
        actual_wait = wait_after + random.gauss(0, wait_after * 0.25)
        actual_wait = max(1.0, actual_wait)
        time.sleep(actual_wait)
        self._natural_scroll(random.randint(2, 4))
        return True

    def build_referrer_header(self, target_url: str) -> str:
        return NavigationProfile.get_referrer(target_url, self._prev_url)

    # ── 내부 헬퍼 ─────────────────────────────────────────────

    def _goto_step(self, step: NavStep):
        logger.debug(f"[Navigator] 방문: {step.url[:60]} ({step.purpose})")
        referer = NavigationProfile.get_referrer(step.url, self._prev_url)
        try:
            if referer:
                self.page.set_extra_http_headers({"Referer": referer})
            self.page.goto(step.url, wait_until="domcontentloaded", timeout=20000)
            self._prev_url = step.url
        except Exception as e:
            logger.debug(f"[Navigator] step goto 실패: {e}")
            return

        wait = random.uniform(step.wait_min, step.wait_max)
        time.sleep(wait)
        self._natural_scroll(step.scroll_count)

    def _natural_scroll(self, count: int):
        for i in range(count):
            delta = random.randint(200, 600)
            self.page.mouse.wheel(0, delta)
            time.sleep(random.uniform(0.3, 0.8))

        # 10% 확률로 살짝 위로 스크롤 (읽는 척)
        if random.random() < 0.10:
            self.page.mouse.wheel(0, -random.randint(80, 200))
            time.sleep(random.uniform(0.2, 0.5))

    def _harvest_cookies(self) -> Dict[str, str]:
        try:
            raw = self.page.context.cookies()
            return {c['name']: c['value'] for c in raw}
        except Exception:
            return {}
