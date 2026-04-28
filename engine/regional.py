"""
engine/regional.py — 계정/지역 신호 일관성 관리
────────────────────────────────────────────────────────────────────────────
봇 탐지 우회 대상:
  • 계정/지역 신호(Account/Region Signals)
      - Accept-Language가 IP 지역과 불일치 → 의심
      - Timezone 쿠키가 브라우저 timezone과 불일치 → 의심
      - 통화/로케일 쿠키가 없거나 이상한 값 → 의심
  • IP-Timezone 일관성: 한국 IP라면 timezone=Asia/Seoul

제공:
  RegionalProfile  - 지역별 헤더/쿠키/설정 번들
  KRProfile        - 한국 전용 (기본값)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ─── Chrome 버전 × OS 매트릭스 ──────────────────────────────────────────────
# (user_agent, sec-ch-ua, platform)
_CHROME_PROFILES: List[Tuple[str, str, str]] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
        '"Windows"',
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        '"Not_A Brand";v="8", "Chromium";v="123", "Google Chrome";v="123"',
        '"Windows"',
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
        '"macOS"',
    ),
    (
        "Mozilla/5.0 (Windows NT 11.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
        '"Windows"',
    ),
]

# ─── 스크린 해상도 풀 (한국 PC 사용자 분포 기준) ─────────────────────────────
_SCREEN_RESOLUTIONS: List[Tuple[int, int]] = [
    (1920, 1080),  # 가장 일반적
    (1920, 1080),
    (1920, 1080),
    (2560, 1440),
    (1366, 768),
    (1600, 900),
    (1440, 900),
    (3840, 2160),
]


@dataclass
class RegionalProfile:
    """
    지역별 브라우저 신호 번들.
    동일 세션 내에서 일관된 지역 정보를 유지한다.
    """

    locale:        str = "ko-KR"
    timezone:      str = "Asia/Seoul"
    accept_lang:   str = "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
    country_code:  str = "KR"
    currency:      str = "KRW"

    # 현재 세션에 할당된 Chrome 프로필 (랜덤 선택)
    _ua: str   = field(default="", init=False, repr=False)
    _chua: str = field(default="", init=False, repr=False)
    _platform: str = field(default="", init=False, repr=False)
    _screen: Tuple[int, int] = field(default=(1920, 1080), init=False, repr=False)

    def __post_init__(self):
        ua, chua, platform = random.choice(_CHROME_PROFILES)
        self._ua       = ua
        self._chua     = chua
        self._platform = platform
        self._screen   = random.choice(_SCREEN_RESOLUTIONS)

    # ── 헤더 번들 ─────────────────────────────────────────────

    @property
    def user_agent(self) -> str:
        return self._ua

    @property
    def screen_width(self) -> int:
        return self._screen[0]

    @property
    def screen_height(self) -> int:
        return self._screen[1]

    def browser_headers(self, referer: str = "", sec_fetch_site: str = "none") -> Dict[str, str]:
        """Playwright 컨텍스트 / requests 헤더에 사용할 완전한 헤더 딕셔너리."""
        headers: Dict[str, str] = {
            "User-Agent":              self._ua,
            "Accept":                  (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language":         self.accept_lang,
            "Accept-Encoding":         "gzip, deflate, br, zstd",
            "sec-ch-ua":               self._chua,
            "sec-ch-ua-mobile":        "?0",
            "sec-ch-ua-platform":      self._platform,
            "sec-ch-ua-arch":          '"x86"',
            "sec-ch-ua-bitness":       '"64"',
            "sec-ch-ua-full-version-list": self._chua,
            "Sec-Fetch-Dest":          "document",
            "Sec-Fetch-Mode":          "navigate",
            "Sec-Fetch-Site":          sec_fetch_site,
            "Sec-Fetch-User":          "?1",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control":           "max-age=0",
            "DNT":                     "1",
        }
        if referer:
            headers["Referer"] = referer

        return headers

    def playwright_context_options(self, extra_headers: Dict[str, str] = None) -> Dict:
        """Playwright new_context() 에 전달할 옵션 딕셔너리."""
        opts = {
            "viewport":    {"width": self.screen_width, "height": self.screen_height},
            "user_agent":  self._ua,
            "locale":      self.locale,
            "timezone_id": self.timezone,
            "extra_http_headers": self.browser_headers(),
        }
        if extra_headers:
            opts["extra_http_headers"].update(extra_headers)
        return opts

    # ── 지역 쿠키 ─────────────────────────────────────────────

    def regional_cookies(self, domain: str = "") -> Dict[str, str]:
        """도메인에 적합한 지역 신호 쿠키를 반환한다."""
        base: Dict[str, str] = {}

        if "coupang.com" in domain:
            base.update({
                "countryCode":    self.country_code,
                "overCountryCd":  self.country_code,
                "MARKETID":       "WING",
                "lang":           self.locale,
                "isLoginCheck":   "false",
            })
        elif "naver.com" in domain or "shopping.naver.com" in domain:
            base.update({
                "locale":         self.locale.replace("-", "_"),
            })
        elif "gmarket.co.kr" in domain:
            base.update({
                "GKL":            "KR",
                "GLANG":          "KOR",
            })

        return base

    # ── JS 주입 스니펫 ────────────────────────────────────────

    def timezone_spoof_script(self) -> str:
        """브라우저 timezone을 지역 설정과 일치시키는 JS."""
        return f"""
        Object.defineProperty(Intl, 'DateTimeFormat', {{
            value: new Proxy(Intl.DateTimeFormat, {{
                construct(target, args) {{
                    if (args[1] && !args[1].timeZone) {{
                        args[1].timeZone = '{self.timezone}';
                    }} else if (!args[1]) {{
                        args[1] = {{ timeZone: '{self.timezone}' }};
                    }}
                    return new target(...args);
                }}
            }})
        }});
        """

    def locale_spoof_script(self) -> str:
        """navigator.language / navigator.languages 를 지역에 맞게 위장."""
        langs = self.accept_lang.split(',')
        lang_arr = [l.split(';')[0].strip() for l in langs]
        lang_json = str(lang_arr).replace("'", '"')
        primary = lang_arr[0] if lang_arr else self.locale
        return f"""
        Object.defineProperty(navigator, 'language',  {{ get: () => '{primary}' }});
        Object.defineProperty(navigator, 'languages', {{ get: () => {lang_json} }});
        """


# ─── 전역 한국 프로필 (기본) ─────────────────────────────────────────────────

def get_kr_profile() -> RegionalProfile:
    """한국 지역 프로필을 반환한다 (매 호출마다 새 랜덤 UA)."""
    return RegionalProfile(
        locale="ko-KR",
        timezone="Asia/Seoul",
        accept_lang="ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        country_code="KR",
        currency="KRW",
    )
