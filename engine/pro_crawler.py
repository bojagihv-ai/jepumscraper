"""
engine/pro_crawler.py — 전문 크롤링 마스터 오케스트레이터
────────────────────────────────────────────────────────────────────────────
8가지 봇 탐지 신호를 통합 우회:

  1. IP 평판         → ip_manager  : 프록시 로테이션 + 주거용 IP 우선
  2. 세션 나이        → session_manager: 세션 예열 + 쿠키 이력 영속화
  3. 쿠키 이력        → session_manager + bypass_engine: 도메인별 쿠키 축적
  4. TLS/HTTP2 특성  → fingerprint_suite: Chrome 124 JA3/JA4 + H2 SETTINGS
  5. 행동 패턴        → human_behavior: 베지어 마우스 + 관성 스크롤 + 읽기 정지
  6. 요청 속도        → rate_limiter: 토큰 버킷 + 적응형 백오프
  7. 탐색 경로        → navigation: 홈 → 카테고리 → 검색 → 상품 경로
  8. 계정/지역 신호   → regional: 한국 로케일 + 일관된 timezone/Accept-Language

사용법:
    from engine.pro_crawler import ProCrawler

    crawler = ProCrawler()

    # requests 기반 (빠름)
    resp = crawler.fetch("https://www.coupang.com/np/search?q=노트북")

    # Playwright 기반 (JS 렌더링 필요 시)
    html = crawler.fetch_playwright(
        url="https://search.shopping.naver.com/search/all?query=노트북",
        platform="naver",
    )
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

import config

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── 지연 임포트 헬퍼 ───────────────────────────────────────────────────────

def _import_engine(name: str):
    try:
        return __import__(f"engine.{name}", fromlist=[name])
    except ImportError:
        return None


# ─── 응답 래퍼 ───────────────────────────────────────────────────────────────

class ProResponse:
    def __init__(self, html: str, status: int, cookies: Dict[str, str], backend: str):
        self.text        = html
        self.status_code = status
        self.cookies     = cookies
        self.backend     = backend  # 'smart_session'|'playwright'|'requests'

    @property
    def ok(self) -> bool:
        return self.status_code in (200, 206)

    def __bool__(self):
        return self.ok and bool(self.text)


# ─── ProCrawler ───────────────────────────────────────────────────────────────

class ProCrawler:
    """
    8개 신호를 통합한 전문 크롤러.

    Parameters
    ----------
    proxy : str | None
        고정 프록시 (None이면 ip_manager 자동 선택)
    warmup_sessions : bool
        True이면 첫 요청 전 세션을 자동 예열
    playwright_fallback : bool
        True이면 requests 실패 시 Playwright로 자동 폴백
    """

    def __init__(
        self,
        proxy:              Optional[str] = None,
        warmup_sessions:    bool = True,
        playwright_fallback: bool = True,
    ):
        self._fixed_proxy       = proxy
        self._warmup_sessions   = warmup_sessions
        self._pw_fallback       = playwright_fallback

        # 지역 프로필 (세션당 하나, 일관된 신호 유지)
        self._regional    = self._load_regional()
        self._fingerprint = self._load_fingerprint()

    # ── 지역/지문 로드 ────────────────────────────────────────

    def _load_regional(self):
        try:
            from engine.regional import get_kr_profile
            return get_kr_profile()
        except Exception:
            return None

    def _load_fingerprint(self):
        try:
            from engine.fingerprint_suite import get_default_fingerprint
            return get_default_fingerprint()
        except Exception:
            return None

    def _get_proxy(self) -> Optional[str]:
        if self._fixed_proxy:
            return self._fixed_proxy
        try:
            from engine.ip_manager import get_proxy
            return get_proxy()
        except Exception:
            return None

    # ── 공개 API ──────────────────────────────────────────────

    def fetch(
        self,
        url: str,
        referer: str    = "",
        extra_cookies:  Optional[Dict[str, str]] = None,
        platform:       Optional[str] = None,
    ) -> ProResponse:
        """
        requests 계열 (SmartSession)로 URL을 가져온다.
        실패 시 Playwright 폴백.
        """
        # 1) 속도 제한
        self._rate_wait(url)

        # 2) 세션 + 쿠키 준비
        session, browser_sess = self._prepare_session(url, platform)

        # 3) 지역 헤더 + Referer
        headers = {}
        if self._regional:
            sf_site = "same-site" if referer and urlparse(referer).netloc == urlparse(url).netloc else "none"
            headers = self._regional.browser_headers(referer=referer, sec_fetch_site=sf_site)
        if referer:
            headers["Referer"] = referer

        # 4) 쿠키 병합 (세션 쿠키 + bypass 쿠키 + 지역 쿠키 + 추가 쿠키)
        cookies = {}
        if browser_sess:
            from engine.session_manager import CookieHistoryBuilder
            cookies.update(CookieHistoryBuilder.get_cookies_for_request(browser_sess, url))
        if self._regional:
            domain = urlparse(url).netloc
            cookies.update(self._regional.regional_cookies(domain))
        if extra_cookies:
            cookies.update(extra_cookies)

        # 5) bypass 쿠키 획득 (쿠팡 등 Akamai)
        bypass_ck = self._acquire_bypass_cookies(url)
        cookies.update(bypass_ck)

        # 6) 요청 실행
        try:
            raw = session.get(url, headers=headers, cookies=cookies)
            html        = raw.text
            status      = raw.status_code
            resp_cookies = dict(raw.cookies) if hasattr(raw.cookies, 'items') else {}
        except Exception as e:
            logger.warning(f"[ProCrawler] requests 실패: {e}")
            html, status, resp_cookies = "", 0, {}

        # 7) 속도 조절 피드백
        if status:
            self._rate_feedback(url, status)

        # 8) 세션 쿠키 업데이트
        if browser_sess:
            browser_sess.update_cookies(resp_cookies)
            browser_sess.update_cookies(cookies)
            browser_sess.add_visit(url)
            try:
                from engine.session_manager import get_pool
                get_pool().release(browser_sess)
            except Exception:
                pass

        # 9) 차단 감지 → Playwright 폴백
        if self._is_blocked(html, status) and self._pw_fallback:
            logger.info(f"[ProCrawler] 차단 감지 → Playwright 폴백: {url[:60]}")
            html = self.fetch_playwright(url, platform=platform, referer=referer)
            return ProResponse(html, 200 if html else 0, cookies, "playwright")

        backend = getattr(session, '_backend', 'unknown')
        return ProResponse(html, status, {**cookies, **resp_cookies}, backend)

    def fetch_playwright(
        self,
        url: str,
        platform:  Optional[str] = None,
        referer:   str = "",
        wait_secs: float = 3.5,
        extra_cookies: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Playwright + 전체 신호 스택으로 URL을 가져온다.
        반환: HTML 문자열
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("[ProCrawler] Playwright 미설치")
            return ""

        # 스텔스 스크립트
        stealth = self._build_stealth_script()
        proxy = self._get_proxy()
        pw_proxy = None
        if proxy:
            from urllib.parse import urlparse as _up
            p = _up(proxy)
            pw_proxy = {
                "server": f"{p.scheme}://{p.hostname}:{p.port}",
                **({"username": p.username, "password": p.password}
                   if p.username else {}),
            }

        html = ""
        browser = None
        context = None
        tmp_profile = ""
        with sync_playwright() as pw:
            try:
                launch_args = self._launch_args()
                launch_kwargs = {
                    "headless": True,
                    "args": launch_args,
                }
                if pw_proxy:
                    launch_kwargs["proxy"] = pw_proxy

                # Browser is launched after deciding whether to reuse a copied Chrome profile.

                # 컨텍스트 옵션 (지역 신호 포함)
                ctx_opts = {
                    "viewport": {"width": 1920, "height": 1080},
                    "user_agent": (
                        self._regional.user_agent if self._regional
                        else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "locale":      "ko-KR",
                    "timezone_id": "Asia/Seoul",
                }
                if self._regional:
                    ctx_opts["extra_http_headers"] = self._regional.browser_headers(
                        referer=referer
                    )

                try:
                    from engine.browser_profile import (
                        chrome_profile_name,
                        coupang_browser_profile_dir,
                        copy_chrome_profile_tmp,
                        use_user_browser_session,
                    )
                    is_coupang = (platform or "").lower() == "coupang" or "coupang.com" in urlparse(url).netloc
                    if is_coupang and getattr(config, "COUPANG_USE_DEDICATED_PROFILE", True):
                        profile_dir = coupang_browser_profile_dir()
                        logger.info("[ProCrawler] Coupang dedicated Chrome profile: %s", profile_dir)
                        context = pw.chromium.launch_persistent_context(
                            user_data_dir=str(profile_dir),
                            channel="chrome",
                            headless=False,
                            args=launch_args,
                            **({"proxy": pw_proxy} if pw_proxy else {}),
                            **ctx_opts,
                        )
                    elif use_user_browser_session():
                        tmp_profile = copy_chrome_profile_tmp()
                        if tmp_profile:
                            context = pw.chromium.launch_persistent_context(
                                user_data_dir=tmp_profile,
                                channel="chrome",
                                headless=False,
                                args=launch_args + [f"--profile-directory={chrome_profile_name()}"],
                                **({"proxy": pw_proxy} if pw_proxy else {}),
                                **ctx_opts,
                            )
                except Exception as e:
                    logger.debug(f"[ProCrawler] Chrome profile context skipped: {e}")

                if context is None:
                    browser = pw.chromium.launch(**launch_kwargs)
                    context = browser.new_context(**ctx_opts)

                # bypass + 지역 쿠키 주입
                cookies_to_inject = self._build_playwright_cookies(url, extra_cookies)
                if cookies_to_inject:
                    try:
                        context.add_cookies(cookies_to_inject)
                    except Exception as e:
                        logger.debug(f"[ProCrawler] 쿠키 주입 실패: {e}")

                page = context.new_page()
                page.add_init_script(stealth)

                # 탐색 경로 실행 (예열)
                from engine.navigation import PlaywrightNavigator
                nav = PlaywrightNavigator(page)

                if self._warmup_sessions:
                    logger.info(f"[ProCrawler] 예열 탐색 시작: {url[:50]}")
                    warmup_cookies = nav.warmup(url)
                    # 예열에서 획득한 쿠키를 context에 주입
                    if warmup_cookies:
                        inject_list = [
                            {"name": k, "value": v,
                             "domain": f".{urlparse(url).netloc.lstrip('www.')}",
                             "path": "/"}
                            for k, v in warmup_cookies.items()
                            if k and v
                        ]
                        try:
                            context.add_cookies(inject_list)
                        except Exception:
                            pass

                # 목표 페이지 이동
                referer_hdr = nav.build_referrer_header(url)
                if referer_hdr:
                    page.set_extra_http_headers({"Referer": referer_hdr})

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as eg:
                    logger.warning(f"[ProCrawler] goto 실패: {eg}")

                # 속도 조절 대기
                actual_wait = wait_secs + random.gauss(0, wait_secs * 0.2)
                time.sleep(max(2.0, actual_wait))

                # CAPTCHA 자동 해결
                try:
                    from engine.captcha import get_solver
                    solver = get_solver()
                    if solver.available():
                        if solver.auto_solve_page(page):
                            logger.info("[ProCrawler] CAPTCHA 자동 해결 완료")
                            time.sleep(2.0)
                except Exception:
                    pass

                # 인간다운 읽기 행동
                from engine.human_behavior import SyncHumanBehavior
                hb = SyncHumanBehavior()
                hb.reading_session(page, duration=random.uniform(2.5, 4.5))

                html = page.content()

                # 결과 쿠키 수집 → 세션 저장
                try:
                    raw_cookies = context.cookies()
                    ck_dict     = {c['name']: c['value'] for c in raw_cookies}
                    self._save_session_cookies(url, ck_dict)
                except Exception:
                    pass

                context.close()
                if browser:
                    if context:
                        context.close()
                    if browser:
                        browser.close()
                if tmp_profile:
                    import shutil
                    shutil.rmtree(tmp_profile, ignore_errors=True)

            except Exception as e:
                logger.error(f"[ProCrawler] Playwright 오류: {e}")
                try:
                    browser.close()
                except Exception:
                    pass
                if tmp_profile:
                    import shutil
                    shutil.rmtree(tmp_profile, ignore_errors=True)

        return html

    # ── 내부 헬퍼 ─────────────────────────────────────────────

    def _prepare_session(self, url: str, platform: Optional[str]):
        """SmartSession + BrowserSession 준비."""
        proxy = self._get_proxy()

        # BrowserSession 가져오기 (세션 나이 + 쿠키 이력)
        browser_sess = None
        try:
            from engine.session_manager import get_pool
            domain = urlparse(url).netloc
            pool   = get_pool()
            browser_sess = pool.get(domain, require_warm=True)
        except Exception:
            pass

        # SmartSession (TLS 지문 위장)
        try:
            from engine.smart_session import SmartSession
            session = SmartSession(proxy=proxy)
        except Exception:
            import requests
            session = requests.Session()

        return session, browser_sess

    def _acquire_bypass_cookies(self, url: str) -> Dict[str, str]:
        """bypass_engine으로 플랫폼별 우회 쿠키 획득."""
        try:
            from engine.bypass_engine import get_bypass_cookies, detect_protection
            domain = urlparse(url).netloc
            if "coupang.com" in domain:
                return get_bypass_cookies(url, protection='akamai')
            # 기타 도메인은 auto-detect
            return {}
        except Exception:
            return {}

    def _build_playwright_cookies(
        self,
        url: str,
        extra: Optional[Dict[str, str]] = None,
    ) -> List[dict]:
        """Playwright context에 주입할 쿠키 리스트 생성."""
        domain = "." + urlparse(url).netloc.lstrip("www.")
        merged: Dict[str, str] = {}

        # 지역 쿠키
        if self._regional:
            merged.update(self._regional.regional_cookies(domain))

        # bypass 쿠키
        merged.update(self._acquire_bypass_cookies(url))

        # 세션 쿠키
        try:
            from engine.session_manager import get_pool
            pool = get_pool()
            sess = pool.get(domain.lstrip('.'), require_warm=False)
            merged.update(sess.cookies)
        except Exception:
            pass

        if extra:
            merged.update(extra)

        return [
            {"name": k, "value": v, "domain": domain, "path": "/"}
            for k, v in merged.items()
            if k and v
        ]

    def _build_stealth_script(self) -> str:
        """완전한 스텔스 스크립트 생성 (지역 신호 포함)."""
        try:
            from engine.stealth import get_full_stealth_script
            base = get_full_stealth_script()
        except Exception:
            base = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

        # 지역 신호 스크립트 추가
        regional_script = ""
        if self._regional:
            regional_script += self._regional.locale_spoof_script()
            regional_script += self._regional.timezone_spoof_script()

        # 지문 추가 스크립트
        fp_script = ""
        if self._fingerprint:
            fp_script = self._fingerprint.playwright_stealth_additions()

        return base + "\n" + regional_script + "\n" + fp_script

    def _is_blocked(self, html: str, status: int) -> bool:
        try:
            from engine.bypass_engine import is_blocked
            return is_blocked(html, status)
        except Exception:
            if not html or status in (403, 429, 503):
                return True
            keywords = ["Access Denied", "captcha", "차단", "비정상"]
            lower    = html.lower()
            return any(k.lower() in lower for k in keywords)

    def _rate_wait(self, url: str):
        try:
            from engine.rate_limiter import wait
            wait(url)
        except Exception:
            pass

    def _rate_feedback(self, url: str, status: int):
        try:
            from engine.rate_limiter import on_response
            on_response(url, status)
        except Exception:
            pass

    def _save_session_cookies(self, url: str, cookies: Dict[str, str]):
        try:
            from engine.session_manager import get_pool
            domain = urlparse(url).netloc
            pool   = get_pool()
            sess   = pool.get(domain, require_warm=False)
            pool.mark_warm(sess, cookies)
        except Exception:
            pass

    @staticmethod
    def _launch_args() -> List[str]:
        return [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--lang=ko-KR",
            "--window-size=1920,1080",
            "--disable-web-security",
            "--allow-running-insecure-content",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-features=TranslateUI",
            "--force-color-profile=srgb",
            "--metrics-recording-only",
            "--disable-default-apps",
        ]


# ─── 전역 싱글톤 ─────────────────────────────────────────────────────────────

_CRAWLER: Optional[ProCrawler] = None


def get_crawler(**kwargs) -> ProCrawler:
    """전역 ProCrawler 인스턴스 반환."""
    global _CRAWLER
    if _CRAWLER is None:
        _CRAWLER = ProCrawler(**kwargs)
    return _CRAWLER
