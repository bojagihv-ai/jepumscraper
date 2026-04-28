"""
engine/captcha.py - CAPTCHA 자동 해결 엔진
──────────────────────────────────────────────────────────────────────────────
지원 유형:
  • reCAPTCHA v2 / v3
  • hCaptcha
  • Cloudflare Turnstile
  • 이미지 CAPTCHA (base64 OCR → 2captcha/capsolver)
  • 오디오 CAPTCHA (자동 폴백)

지원 서비스 (우선순위 순):
  1. Capsolver  (가장 빠름, Cloudflare Turnstile 지원 우수)
  2. 2captcha   (범용, 24/7)
  3. Anti-Captcha

설정:
  config.CAPSOLVER_API_KEY  or  env CAPSOLVER_API_KEY
  config.TWOCAPTCHA_API_KEY or  env TWOCAPTCHA_API_KEY
  config.ANTICAPTCHA_API_KEY or env ANTICAPTCHA_API_KEY

사용법:
    from engine.captcha import CaptchaSolver

    solver = CaptchaSolver()
    token = solver.solve_recaptcha_v2(site_key="...", page_url="https://...")
    token = solver.solve_turnstile(site_key="...", page_url="https://...")
    token = solver.solve_hcaptcha(site_key="...", page_url="https://...")
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ─── API 키 (config → 환경변수 순서로 읽기) ─────────────────────────────────
def _get_key(name: str) -> str:
    try:
        import config
        val = getattr(config, name, "") or ""
        if val:
            return val.strip()
    except ImportError:
        pass
    return os.environ.get(name, "").strip()


CAPSOLVER_KEY    = _get_key("CAPSOLVER_API_KEY")
TWOCAPTCHA_KEY   = _get_key("TWOCAPTCHA_API_KEY")
ANTICAPTCHA_KEY  = _get_key("ANTICAPTCHA_API_KEY")

# ─── 대기 설정 ───────────────────────────────────────────────────────────────
_POLL_INTERVAL = 5   # 초 (폴링 주기)
_MAX_WAIT      = 180 # 초 (최대 대기)


# ══════════════════════════════════════════════════════════════════════════════
# 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def _capsolver_post(payload: dict) -> dict:
    """Capsolver API POST 헬퍼."""
    import requests
    resp = requests.post(
        "https://api.capsolver.com/createTask",
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _capsolver_get_result(task_id: str) -> Optional[str]:
    """Capsolver 작업 결과 폴링."""
    import requests
    deadline = time.time() + _MAX_WAIT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        resp = requests.post(
            "https://api.capsolver.com/getTaskResult",
            json={"clientKey": CAPSOLVER_KEY, "taskId": task_id},
            timeout=30,
        )
        data = resp.json()
        status = data.get("status", "")
        if status == "ready":
            solution = data.get("solution", {})
            return (
                solution.get("token")
                or solution.get("gRecaptchaResponse")
                or solution.get("userAgent")
                or None
            )
        if status == "failed":
            err = data.get("errorDescription", "unknown")
            logger.warning(f"[Capsolver] 작업 실패: {err}")
            return None
    logger.warning("[Capsolver] 타임아웃")
    return None


def _2captcha_post(payload: dict) -> Optional[str]:
    """2captcha 작업 제출 → task_id 반환."""
    import requests
    resp = requests.post(
        "http://2captcha.com/in.php",
        data={**payload, "key": TWOCAPTCHA_KEY, "json": 1},
        timeout=30,
    )
    data = resp.json()
    if data.get("status") == 1:
        return str(data.get("request", ""))
    logger.warning(f"[2captcha] 제출 실패: {data}")
    return None


def _2captcha_get_result(task_id: str) -> Optional[str]:
    """2captcha 결과 폴링."""
    import requests
    deadline = time.time() + _MAX_WAIT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        resp = requests.get(
            "http://2captcha.com/res.php",
            params={"key": TWOCAPTCHA_KEY, "action": "get", "id": task_id, "json": 1},
            timeout=30,
        )
        data = resp.json()
        status = data.get("status", 0)
        req = data.get("request", "")
        if status == 1:
            return req
        if req not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
            logger.warning(f"[2captcha] 오류: {req}")
            return None
    logger.warning("[2captcha] 타임아웃")
    return None


def _anticaptcha_post(payload: dict) -> Optional[str]:
    """Anti-Captcha 작업 제출 → task_id 반환."""
    import requests
    body = {"clientKey": ANTICAPTCHA_KEY, "task": payload}
    resp = requests.post(
        "https://api.anti-captcha.com/createTask",
        json=body,
        timeout=30,
    )
    data = resp.json()
    if data.get("errorId") == 0:
        return str(data.get("taskId", ""))
    logger.warning(f"[Anti-Captcha] 제출 실패: {data.get('errorDescription')}")
    return None


def _anticaptcha_get_result(task_id: str) -> Optional[str]:
    """Anti-Captcha 결과 폴링."""
    import requests
    deadline = time.time() + _MAX_WAIT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        resp = requests.post(
            "https://api.anti-captcha.com/getTaskResult",
            json={"clientKey": ANTICAPTCHA_KEY, "taskId": int(task_id)},
            timeout=30,
        )
        data = resp.json()
        if data.get("status") == "ready":
            sol = data.get("solution", {})
            return sol.get("gRecaptchaResponse") or sol.get("token") or None
        if data.get("errorId", 0) != 0:
            logger.warning(f"[Anti-Captcha] 오류: {data.get('errorDescription')}")
            return None
    logger.warning("[Anti-Captcha] 타임아웃")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# CaptchaSolver 클래스
# ══════════════════════════════════════════════════════════════════════════════

class CaptchaSolver:
    """
    CAPTCHA 자동 해결기.

    사용 가능한 API 키 기준으로 서비스를 자동 선택한다.
    선호 서비스를 고정하려면 `preferred` 인수를 사용하라:
        CaptchaSolver(preferred="capsolver")
        CaptchaSolver(preferred="2captcha")
        CaptchaSolver(preferred="anticaptcha")
    """

    def __init__(self, preferred: Optional[str] = None):
        self._service = self._pick_service(preferred)
        if self._service:
            logger.info(f"[CaptchaSolver] 서비스: {self._service}")
        else:
            logger.warning("[CaptchaSolver] 사용 가능한 CAPTCHA 서비스 API 키 없음")

    def _pick_service(self, preferred: Optional[str]) -> Optional[str]:
        """우선순위: preferred → capsolver → 2captcha → anticaptcha."""
        order = ["capsolver", "2captcha", "anticaptcha"]
        if preferred and preferred in order:
            order = [preferred] + [s for s in order if s != preferred]

        for svc in order:
            if svc == "capsolver" and CAPSOLVER_KEY:
                return "capsolver"
            if svc == "2captcha" and TWOCAPTCHA_KEY:
                return "2captcha"
            if svc == "anticaptcha" and ANTICAPTCHA_KEY:
                return "anticaptcha"
        return None

    def available(self) -> bool:
        return self._service is not None

    # ──────────────────────────────────────────────────────────────────────
    # reCAPTCHA v2
    # ──────────────────────────────────────────────────────────────────────

    def solve_recaptcha_v2(
        self,
        site_key: str,
        page_url: str,
        invisible: bool = False,
        proxy: Optional[str] = None,
    ) -> Optional[str]:
        """reCAPTCHA v2 토큰을 반환한다."""
        logger.info(f"[CaptchaSolver] reCAPTCHA v2 해결 중: {page_url}")

        if self._service == "capsolver":
            task_type = "ReCaptchaV2Task" if proxy else "ReCaptchaV2TaskProxyLess"
            task: dict = {
                "type": task_type,
                "websiteURL": page_url,
                "websiteKey": site_key,
                "isInvisible": invisible,
            }
            if proxy:
                task.update(self._proxy_fields(proxy))
            payload = {"clientKey": CAPSOLVER_KEY, "task": task}
            data = _capsolver_post(payload)
            if data.get("errorId", 1) != 0:
                logger.warning(f"[Capsolver] reCAPTCHA v2 제출 실패: {data.get('errorDescription')}")
                return None
            return _capsolver_get_result(data["taskId"])

        if self._service == "2captcha":
            task_id = _2captcha_post({
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "invisible": int(invisible),
            })
            if not task_id:
                return None
            return _2captcha_get_result(task_id)

        if self._service == "anticaptcha":
            task_id = _anticaptcha_post({
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "isInvisible": invisible,
            })
            if not task_id:
                return None
            return _anticaptcha_get_result(task_id)

        return None

    # ──────────────────────────────────────────────────────────────────────
    # reCAPTCHA v3
    # ──────────────────────────────────────────────────────────────────────

    def solve_recaptcha_v3(
        self,
        site_key: str,
        page_url: str,
        action: str = "verify",
        min_score: float = 0.7,
    ) -> Optional[str]:
        """reCAPTCHA v3 토큰을 반환한다."""
        logger.info(f"[CaptchaSolver] reCAPTCHA v3 해결 중: {page_url} (action={action})")

        if self._service == "capsolver":
            payload = {
                "clientKey": CAPSOLVER_KEY,
                "task": {
                    "type": "ReCaptchaV3TaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                    "pageAction": action,
                    "minScore": min_score,
                },
            }
            data = _capsolver_post(payload)
            if data.get("errorId", 1) != 0:
                logger.warning(f"[Capsolver] reCAPTCHA v3 제출 실패: {data.get('errorDescription')}")
                return None
            return _capsolver_get_result(data["taskId"])

        if self._service == "2captcha":
            task_id = _2captcha_post({
                "method": "userrecaptcha",
                "version": "v3",
                "googlekey": site_key,
                "pageurl": page_url,
                "action": action,
                "min_score": min_score,
            })
            if not task_id:
                return None
            return _2captcha_get_result(task_id)

        if self._service == "anticaptcha":
            task_id = _anticaptcha_post({
                "type": "RecaptchaV3TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "minScore": min_score,
                "pageAction": action,
            })
            if not task_id:
                return None
            return _anticaptcha_get_result(task_id)

        return None

    # ──────────────────────────────────────────────────────────────────────
    # hCaptcha
    # ──────────────────────────────────────────────────────────────────────

    def solve_hcaptcha(
        self,
        site_key: str,
        page_url: str,
        proxy: Optional[str] = None,
    ) -> Optional[str]:
        """hCaptcha 토큰을 반환한다."""
        logger.info(f"[CaptchaSolver] hCaptcha 해결 중: {page_url}")

        if self._service == "capsolver":
            task_type = "HCaptchaTask" if proxy else "HCaptchaTaskProxyLess"
            task = {
                "type": task_type,
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
            if proxy:
                task.update(self._proxy_fields(proxy))
            payload = {"clientKey": CAPSOLVER_KEY, "task": task}
            data = _capsolver_post(payload)
            if data.get("errorId", 1) != 0:
                logger.warning(f"[Capsolver] hCaptcha 제출 실패: {data.get('errorDescription')}")
                return None
            return _capsolver_get_result(data["taskId"])

        if self._service == "2captcha":
            task_id = _2captcha_post({
                "method": "hcaptcha",
                "sitekey": site_key,
                "pageurl": page_url,
            })
            if not task_id:
                return None
            return _2captcha_get_result(task_id)

        if self._service == "anticaptcha":
            task_id = _anticaptcha_post({
                "type": "HCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            })
            if not task_id:
                return None
            return _anticaptcha_get_result(task_id)

        return None

    # ──────────────────────────────────────────────────────────────────────
    # Cloudflare Turnstile
    # ──────────────────────────────────────────────────────────────────────

    def solve_turnstile(
        self,
        site_key: str,
        page_url: str,
        action: Optional[str] = None,
        cdata: Optional[str] = None,
    ) -> Optional[str]:
        """Cloudflare Turnstile 토큰을 반환한다."""
        logger.info(f"[CaptchaSolver] Turnstile 해결 중: {page_url}")

        if self._service == "capsolver":
            task: dict = {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
            meta: dict = {}
            if action:
                meta["action"] = action
            if cdata:
                meta["cdata"] = cdata
            if meta:
                task["metadata"] = meta
            payload = {"clientKey": CAPSOLVER_KEY, "task": task}
            data = _capsolver_post(payload)
            if data.get("errorId", 1) != 0:
                logger.warning(f"[Capsolver] Turnstile 제출 실패: {data.get('errorDescription')}")
                return None
            return _capsolver_get_result(data["taskId"])

        if self._service == "2captcha":
            task_id = _2captcha_post({
                "method": "turnstile",
                "sitekey": site_key,
                "pageurl": page_url,
                **({"action": action} if action else {}),
                **({"data": cdata} if cdata else {}),
            })
            if not task_id:
                return None
            return _2captcha_get_result(task_id)

        if self._service == "anticaptcha":
            task_id = _anticaptcha_post({
                "type": "TurnstileTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            })
            if not task_id:
                return None
            return _anticaptcha_get_result(task_id)

        return None

    # ──────────────────────────────────────────────────────────────────────
    # 이미지 CAPTCHA
    # ──────────────────────────────────────────────────────────────────────

    def solve_image_captcha(
        self,
        image_path: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        image_b64: Optional[str] = None,
        case_sensitive: bool = False,
    ) -> Optional[str]:
        """이미지 CAPTCHA 텍스트를 반환한다."""
        if image_path:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
        if image_bytes:
            image_b64 = base64.b64encode(image_bytes).decode()
        if not image_b64:
            logger.warning("[CaptchaSolver] 이미지 데이터 없음")
            return None

        logger.info("[CaptchaSolver] 이미지 CAPTCHA 해결 중...")

        if self._service == "capsolver":
            payload = {
                "clientKey": CAPSOLVER_KEY,
                "task": {
                    "type": "ImageToTextTask",
                    "body": image_b64,
                    "case": case_sensitive,
                },
            }
            data = _capsolver_post(payload)
            if data.get("errorId", 1) != 0:
                return None
            return _capsolver_get_result(data["taskId"])

        if self._service == "2captcha":
            task_id = _2captcha_post({
                "method": "base64",
                "body": image_b64,
                "regsense": int(case_sensitive),
            })
            if not task_id:
                return None
            return _2captcha_get_result(task_id)

        if self._service == "anticaptcha":
            task_id = _anticaptcha_post({
                "type": "ImageToTextTask",
                "body": image_b64,
                "case": case_sensitive,
            })
            if not task_id:
                return None
            return _anticaptcha_get_result(task_id)

        return None

    # ──────────────────────────────────────────────────────────────────────
    # Playwright 통합 헬퍼
    # ──────────────────────────────────────────────────────────────────────

    def inject_recaptcha_token(self, page, token: str):
        """
        Playwright 페이지에 reCAPTCHA v2 토큰을 주입한다.
        서버 측 검증 전에 호출해야 한다.
        """
        page.evaluate(f"""
            document.getElementById('g-recaptcha-response').value = '{token}';
            document.getElementById('g-recaptcha-response').style.display = 'block';
        """)

    def inject_hcaptcha_token(self, page, token: str):
        """Playwright 페이지에 hCaptcha 토큰을 주입한다."""
        page.evaluate(f"""
            document.querySelector('[name="h-captcha-response"]').value = '{token}';
        """)

    def auto_solve_page(self, page) -> bool:
        """
        Playwright 페이지에서 CAPTCHA를 자동 감지 + 해결한다.
        성공 시 True 반환.
        """
        if not self.available():
            return False

        html = page.content()
        current_url = page.url

        # reCAPTCHA v2 감지
        import re
        m = re.search(r"data-sitekey=['\"]([^'\"]{20,})['\"]", html)
        if m and "g-recaptcha" in html:
            site_key = m.group(1)
            logger.info(f"[CaptchaSolver] 페이지에서 reCAPTCHA v2 감지: {site_key[:20]}...")
            token = self.solve_recaptcha_v2(site_key, current_url)
            if token:
                try:
                    self.inject_recaptcha_token(page, token)
                    return True
                except Exception as e:
                    logger.warning(f"[CaptchaSolver] 토큰 주입 실패: {e}")

        # hCaptcha 감지
        m = re.search(r"data-sitekey=['\"]([^'\"]{20,})['\"]", html)
        if m and "hcaptcha" in html:
            site_key = m.group(1)
            logger.info(f"[CaptchaSolver] 페이지에서 hCaptcha 감지: {site_key[:20]}...")
            token = self.solve_hcaptcha(site_key, current_url)
            if token:
                try:
                    self.inject_hcaptcha_token(page, token)
                    return True
                except Exception as e:
                    logger.warning(f"[CaptchaSolver] hCaptcha 토큰 주입 실패: {e}")

        # Turnstile 감지
        m = re.search(r"data-sitekey=['\"]([0-9a-zA-Z_\-]{10,})['\"]", html)
        if m and ("turnstile" in html or "cf-challenge" in html.lower()):
            site_key = m.group(1)
            logger.info(f"[CaptchaSolver] 페이지에서 Turnstile 감지: {site_key[:20]}...")
            token = self.solve_turnstile(site_key, current_url)
            if token:
                try:
                    page.evaluate(f"""
                        const el = document.querySelector('[name="cf-turnstile-response"]');
                        if(el) el.value = '{token}';
                    """)
                    return True
                except Exception as e:
                    logger.warning(f"[CaptchaSolver] Turnstile 토큰 주입 실패: {e}")

        return False

    # ──────────────────────────────────────────────────────────────────────
    # 내부 유틸
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _proxy_fields(proxy_url: str) -> dict:
        """프록시 URL을 Capsolver 필드로 파싱한다."""
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        return {
            "proxyType": p.scheme or "http",
            "proxyAddress": p.hostname or "",
            "proxyPort": p.port or 3128,
            "proxyLogin": p.username or "",
            "proxyPassword": p.password or "",
        }


# ─── 전역 단일 인스턴스 ──────────────────────────────────────────────────────
_SOLVER: Optional[CaptchaSolver] = None


def get_solver() -> CaptchaSolver:
    """전역 CaptchaSolver 인스턴스를 반환한다."""
    global _SOLVER
    if _SOLVER is None:
        _SOLVER = CaptchaSolver()
    return _SOLVER
