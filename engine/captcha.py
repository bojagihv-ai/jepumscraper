"""
engine/captcha.py - CAPTCHA 자동 해결 엔진
──────────────────────────────────────────────────────────────────────────────
지원 유형:
  • reCAPTCHA v2 / v3
  • hCaptcha
  • Cloudflare Turnstile
  • 이미지 CAPTCHA (base64 OCR → 2captcha/capsolver)

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
import json
import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ─── 대기 설정 ───────────────────────────────────────────────────────────────
_POLL_INTERVAL = 5   # 초 (폴링 주기)
_MAX_WAIT      = 180 # 초 (최대 대기)


# ─── API 키 (매 호출마다 최신 설정을 읽음) ──────────────────────────────────
def _get_key(name: str) -> str:
    """user_settings.json → config → 환경변수 순으로 API 키를 읽는다."""
    # user_settings.json 우선 (실행 중 변경 반영)
    try:
        import os as _os
        settings_file = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "user_settings.json")
        if _os.path.exists(settings_file):
            import json as _json
            with open(settings_file, "r", encoding="utf-8") as f:
                s = _json.load(f)
            key_map = {
                "CAPSOLVER_API_KEY":   s.get("api_keys", {}).get("capsolver_api_key", ""),
                "TWOCAPTCHA_API_KEY":  s.get("api_keys", {}).get("twocaptcha_api_key", ""),
                "ANTICAPTCHA_API_KEY": s.get("api_keys", {}).get("anticaptcha_api_key", ""),
                "NOPECHA_API_KEY":     s.get("api_keys", {}).get("nopecha_api_key", ""),
                "EZCAPTCHA_API_KEY":   s.get("api_keys", {}).get("ezcaptcha_api_key", ""),
            }
            val = key_map.get(name, "")
            if val and val.strip():
                return val.strip()
    except Exception:
        pass
    # config.py fallback
    try:
        import config
        val = getattr(config, name, "") or ""
        if val:
            return val.strip()
    except ImportError:
        pass
    return os.environ.get(name, "").strip()


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


def _capsolver_get_result(task_id: str, api_key: str) -> Optional[str]:
    """Capsolver 작업 결과 폴링."""
    import requests
    deadline = time.time() + _MAX_WAIT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        try:
            resp = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[Capsolver] 결과 조회 오류: {e}")
            continue

        status = data.get("status", "")
        if status == "ready":
            solution = data.get("solution", {})
            # 주의: userAgent 는 토큰이 아님 — 절대 반환하지 않음
            token = (
                solution.get("token")
                or solution.get("gRecaptchaResponse")
                or solution.get("text")       # ImageToText
                or None
            )
            return token
        if status == "failed":
            err = data.get("errorDescription", "unknown")
            logger.warning(f"[Capsolver] 작업 실패: {err}")
            return None
    logger.warning("[Capsolver] 타임아웃")
    return None


def _2captcha_post(payload: dict, api_key: str) -> Optional[str]:
    """2captcha 작업 제출 → task_id 반환."""
    import requests
    resp = requests.post(
        "https://2captcha.com/in.php",   # HTTPS
        data={**payload, "key": api_key, "json": 1},
        timeout=30,
    )
    data = resp.json()
    if data.get("status") == 1:
        return str(data.get("request", ""))
    logger.warning(f"[2captcha] 제출 실패: {data}")
    return None


def _2captcha_get_result(task_id: str, api_key: str) -> Optional[str]:
    """2captcha 결과 폴링."""
    import requests
    deadline = time.time() + _MAX_WAIT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        resp = requests.get(
            "https://2captcha.com/res.php",   # HTTPS
            params={"key": api_key, "action": "get", "id": task_id, "json": 1},
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


def _anticaptcha_post(payload: dict, api_key: str) -> Optional[str]:
    """Anti-Captcha 작업 제출 → task_id 반환."""
    import requests
    body = {"clientKey": api_key, "task": payload}
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


def _ezcaptcha_post(payload: dict, api_key: str) -> Optional[str]:
    """EzCaptcha 작업 제출 → task_id 반환 (Anti-Captcha 호환 API)."""
    import requests
    body = {"clientKey": api_key, "task": payload}
    resp = requests.post(
        "https://api.ez-captcha.com/createTask",
        json=body,
        timeout=30,
    )
    data = resp.json()
    if data.get("errorId") == 0:
        return str(data.get("taskId", ""))
    logger.warning(f"[EzCaptcha] 제출 실패: {data.get('errorDescription')}")
    return None


def _ezcaptcha_get_result(task_id: str, api_key: str) -> Optional[str]:
    """EzCaptcha 결과 폴링 (Anti-Captcha 호환 API)."""
    import requests
    try:
        task_id_int = int(task_id)
    except (ValueError, TypeError):
        logger.warning(f"[EzCaptcha] 잘못된 task_id: {task_id!r}")
        return None

    deadline = time.time() + _MAX_WAIT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        try:
            resp = requests.post(
                "https://api.ez-captcha.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id_int},
                timeout=30,
            )
            data = resp.json()
        except Exception as e:
            logger.warning(f"[EzCaptcha] 결과 조회 오류: {e}")
            continue

        if data.get("status") == "ready":
            sol = data.get("solution", {})
            return (
                sol.get("gRecaptchaResponse")
                or sol.get("token")
                or sol.get("text")
                or None
            )
        if data.get("errorId", 0) != 0:
            logger.warning(f"[EzCaptcha] 오류: {data.get('errorDescription')}")
            return None
    logger.warning("[EzCaptcha] 타임아웃")
    return None


def _nopecha_solve_token(
    api_key: str,
    captcha_type: str,   # "recaptchav2" | "recaptchav3" | "hcaptcha" | "turnstile"
    sitekey: str,
    url: str,
    **extra,
) -> Optional[str]:
    """
    NopeCHA Cloud Token API 호출 (제출 → 폴링).

    captcha_type: recaptchav2 | recaptchav3 | hcaptcha | turnstile
    extra kwargs (선택): action, min_score, enterprise 등
    """
    import requests

    # 1) 작업 제출
    payload = {
        "key":     api_key,
        "type":    captcha_type,
        "sitekey": sitekey,
        "url":     url,
        **extra,
    }
    try:
        resp = requests.post("https://api.nopecha.com/token", json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"[NopeCHA] 제출 오류: {e}")
        return None

    if data.get("status") != 0:
        logger.warning(f"[NopeCHA] 제출 실패: status={data.get('status')} msg={data.get('data')}")
        return None

    job_id = data.get("data")
    if not job_id or not isinstance(job_id, str):
        logger.warning(f"[NopeCHA] job_id 없음: {data}")
        return None

    # 2) 결과 폴링
    deadline = time.time() + _MAX_WAIT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        try:
            resp = requests.get(
                "https://api.nopecha.com/token",
                params={"key": api_key, "id": job_id},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[NopeCHA] 결과 조회 오류: {e}")
            continue

        status = data.get("status", -1)
        if status == 0:
            tokens = data.get("data", [])
            if isinstance(tokens, list) and tokens:
                return tokens[0]
            if isinstance(tokens, str) and tokens:
                return tokens
            logger.warning(f"[NopeCHA] 토큰 비어 있음: {data}")
            return None
        if status == 7:
            continue  # 아직 처리 중
        logger.warning(f"[NopeCHA] 오류 status={status}: {data.get('data')}")
        return None

    logger.warning("[NopeCHA] 타임아웃")
    return None


def _anticaptcha_get_result(task_id: str, api_key: str) -> Optional[str]:
    """Anti-Captcha 결과 폴링."""
    import requests
    # int 변환 실패 방지
    try:
        task_id_int = int(task_id)
    except (ValueError, TypeError):
        logger.warning(f"[Anti-Captcha] 잘못된 task_id: {task_id!r}")
        return None

    deadline = time.time() + _MAX_WAIT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        try:
            resp = requests.post(
                "https://api.anti-captcha.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id_int},
                timeout=30,
            )
            data = resp.json()
        except Exception as e:
            logger.warning(f"[Anti-Captcha] 결과 조회 오류: {e}")
            continue

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
        # API 키를 매번 최신 설정에서 읽음
        self._capsolver_key   = _get_key("CAPSOLVER_API_KEY")
        self._2captcha_key    = _get_key("TWOCAPTCHA_API_KEY")
        self._anticaptcha_key = _get_key("ANTICAPTCHA_API_KEY")
        self._nopecha_key     = _get_key("NOPECHA_API_KEY")
        self._ezcaptcha_key   = _get_key("EZCAPTCHA_API_KEY")

        self._preferred = preferred
        self._service = self._pick_service(preferred)
        if self._service:
            logger.info(f"[CaptchaSolver] 서비스: {self._service}")
        else:
            logger.warning("[CaptchaSolver] 사용 가능한 CAPTCHA 서비스 API 키 없음")

    def _pick_service(self, preferred: Optional[str]) -> Optional[str]:
        """우선순위: preferred → nopecha → capsolver → ezcaptcha → 2captcha → anticaptcha."""
        order = ["nopecha", "capsolver", "ezcaptcha", "2captcha", "anticaptcha"]
        if preferred and preferred in order:
            order = [preferred] + [s for s in order if s != preferred]

        key_for = {
            "nopecha":    self._nopecha_key,
            "capsolver":  self._capsolver_key,
            "ezcaptcha":  self._ezcaptcha_key,
            "2captcha":   self._2captcha_key,
            "anticaptcha": self._anticaptcha_key,
        }
        for svc in order:
            if key_for.get(svc):
                return svc
        return None

    def available(self) -> bool:
        return self._service is not None

    # ──────────────────────────────────────────────────────────────────────
    # reCAPTCHA v2
    # ──────────────────────────────────────────────────────────────────────

    def _capsolver_submit(self, task: dict) -> Optional[str]:
        """Capsolver: 작업 제출 → taskId 반환 (KeyError 방지)."""
        payload = {"clientKey": self._capsolver_key, "task": task}
        try:
            data = _capsolver_post(payload)
        except Exception as e:
            logger.warning(f"[Capsolver] 제출 오류: {e}")
            return None
        if data.get("errorId", 1) != 0:
            logger.warning(f"[Capsolver] 제출 실패: {data.get('errorDescription')}")
            return None
        task_id = data.get("taskId")
        if not task_id:
            logger.warning(f"[Capsolver] taskId 없음: {data}")
            return None
        return _capsolver_get_result(str(task_id), self._capsolver_key)

    def _try_services(self, fn_map: dict) -> Optional[str]:
        """
        우선순위대로 서비스를 시도하고, 실패 시 다음 서비스로 폴백한다.
        fn_map = {"nopecha": callable, "capsolver": callable, "ezcaptcha": callable,
                  "2captcha": callable, "anticaptcha": callable}
        """
        order = ["nopecha", "capsolver", "ezcaptcha", "2captcha", "anticaptcha"]
        if self._preferred and self._preferred in order:
            order = [self._preferred] + [s for s in order if s != self._preferred]

        key_map = {
            "nopecha":    self._nopecha_key,
            "capsolver":  self._capsolver_key,
            "ezcaptcha":  self._ezcaptcha_key,
            "2captcha":   self._2captcha_key,
            "anticaptcha": self._anticaptcha_key,
        }

        for svc in order:
            if not key_map.get(svc):
                continue
            fn = fn_map.get(svc)
            if not fn:
                continue
            try:
                result = fn()
                if result:
                    logger.info(f"[CaptchaSolver] {svc} 성공")
                    return result
                logger.warning(f"[CaptchaSolver] {svc} 실패, 폴백 시도...")
            except Exception as e:
                logger.warning(f"[CaptchaSolver] {svc} 예외: {e}, 폴백 시도...")
        return None

    def solve_recaptcha_v2(
        self,
        site_key: str,
        page_url: str,
        invisible: bool = False,
        proxy: Optional[str] = None,
    ) -> Optional[str]:
        """reCAPTCHA v2 토큰을 반환한다."""
        logger.info(f"[CaptchaSolver] reCAPTCHA v2 해결 중: {page_url}")

        def _nopecha():
            return _nopecha_solve_token(
                self._nopecha_key, "recaptchav2", site_key, page_url,
                **({"invisible": True} if invisible else {}),
            )

        def _capsolver():
            task_type = "ReCaptchaV2Task" if proxy else "ReCaptchaV2TaskProxyLess"
            task: dict = {
                "type": task_type,
                "websiteURL": page_url,
                "websiteKey": site_key,
                "isInvisible": invisible,
            }
            if proxy:
                task.update(self._proxy_fields(proxy))
            return self._capsolver_submit(task)

        def _ezcap():
            task_id = _ezcaptcha_post({
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "isInvisible": invisible,
            }, self._ezcaptcha_key)
            return _ezcaptcha_get_result(task_id, self._ezcaptcha_key) if task_id else None

        def _2cap():
            task_id = _2captcha_post({
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "invisible": int(invisible),
            }, self._2captcha_key)
            return _2captcha_get_result(task_id, self._2captcha_key) if task_id else None

        def _anti():
            task_id = _anticaptcha_post({
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "isInvisible": invisible,
            }, self._anticaptcha_key)
            return _anticaptcha_get_result(task_id, self._anticaptcha_key) if task_id else None

        return self._try_services({
            "nopecha": _nopecha, "capsolver": _capsolver,
            "ezcaptcha": _ezcap, "2captcha": _2cap, "anticaptcha": _anti,
        })

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

        def _nopecha():
            return _nopecha_solve_token(
                self._nopecha_key, "recaptchav3", site_key, page_url,
                action=action,
            )

        def _capsolver():
            return self._capsolver_submit({
                "type": "ReCaptchaV3TaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "pageAction": action,
                "minScore": min_score,
            })

        def _ezcap():
            task_id = _ezcaptcha_post({
                "type": "RecaptchaV3TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "minScore": min_score,
                "pageAction": action,
            }, self._ezcaptcha_key)
            return _ezcaptcha_get_result(task_id, self._ezcaptcha_key) if task_id else None

        def _2cap():
            task_id = _2captcha_post({
                "method": "userrecaptcha",
                "version": "v3",
                "googlekey": site_key,
                "pageurl": page_url,
                "action": action,
                "min_score": min_score,
            }, self._2captcha_key)
            return _2captcha_get_result(task_id, self._2captcha_key) if task_id else None

        def _anti():
            task_id = _anticaptcha_post({
                "type": "RecaptchaV3TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "minScore": min_score,
                "pageAction": action,
            }, self._anticaptcha_key)
            return _anticaptcha_get_result(task_id, self._anticaptcha_key) if task_id else None

        return self._try_services({
            "nopecha": _nopecha, "capsolver": _capsolver,
            "ezcaptcha": _ezcap, "2captcha": _2cap, "anticaptcha": _anti,
        })

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

        def _nopecha():
            return _nopecha_solve_token(
                self._nopecha_key, "hcaptcha", site_key, page_url,
            )

        def _capsolver():
            task_type = "HCaptchaTask" if proxy else "HCaptchaTaskProxyLess"
            task: dict = {
                "type": task_type,
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
            if proxy:
                task.update(self._proxy_fields(proxy))
            return self._capsolver_submit(task)

        def _ezcap():
            task_id = _ezcaptcha_post({
                "type": "HCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }, self._ezcaptcha_key)
            return _ezcaptcha_get_result(task_id, self._ezcaptcha_key) if task_id else None

        def _2cap():
            task_id = _2captcha_post({
                "method": "hcaptcha",
                "sitekey": site_key,
                "pageurl": page_url,
            }, self._2captcha_key)
            return _2captcha_get_result(task_id, self._2captcha_key) if task_id else None

        def _anti():
            task_id = _anticaptcha_post({
                "type": "HCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }, self._anticaptcha_key)
            return _anticaptcha_get_result(task_id, self._anticaptcha_key) if task_id else None

        return self._try_services({
            "nopecha": _nopecha, "capsolver": _capsolver,
            "ezcaptcha": _ezcap, "2captcha": _2cap, "anticaptcha": _anti,
        })

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

        def _nopecha():
            extra = {}
            if action:
                extra["action"] = action
            if cdata:
                extra["cdata"] = cdata
            return _nopecha_solve_token(
                self._nopecha_key, "turnstile", site_key, page_url, **extra,
            )

        def _capsolver():
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
            return self._capsolver_submit(task)

        def _ezcap():
            task_payload: dict = {
                "type": "TurnstileTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
            if action:
                task_payload["action"] = action
            if cdata:
                task_payload["cdata"] = cdata
            task_id = _ezcaptcha_post(task_payload, self._ezcaptcha_key)
            return _ezcaptcha_get_result(task_id, self._ezcaptcha_key) if task_id else None

        def _2cap():
            task_id = _2captcha_post({
                "method": "turnstile",
                "sitekey": site_key,
                "pageurl": page_url,
                **({"action": action} if action else {}),
                **({"data": cdata} if cdata else {}),
            }, self._2captcha_key)
            return _2captcha_get_result(task_id, self._2captcha_key) if task_id else None

        def _anti():
            task_payload: dict = {
                "type": "TurnstileTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
            if action:
                task_payload["action"] = action
            if cdata:
                task_payload["cdata"] = cdata
            task_id = _anticaptcha_post(task_payload, self._anticaptcha_key)
            return _anticaptcha_get_result(task_id, self._anticaptcha_key) if task_id else None

        return self._try_services({
            "nopecha": _nopecha, "capsolver": _capsolver,
            "ezcaptcha": _ezcap, "2captcha": _2cap, "anticaptcha": _anti,
        })

    # ──────────────────────────────────────────────────────────────────────
    # Geetest v3 / v4
    # ──────────────────────────────────────────────────────────────────────

    def solve_geetest(
        self,
        page_url: str,
        gt: Optional[str] = None,
        challenge: Optional[str] = None,
        captcha_id: Optional[str] = None,
        version: int = 3,
    ) -> Optional[dict]:
        """
        Geetest v3/v4 토큰을 반환한다.
        v3: gt + challenge 필요
        v4: captcha_id 필요
        반환값: {"geetest_challenge": ..., "geetest_validate": ..., "geetest_seccode": ...}
        """
        logger.info(f"[CaptchaSolver] Geetest v{version} 해결 중: {page_url}")

        def _capsolver():
            if version == 4:
                task = {
                    "type": "GeetestTaskProxyLess",
                    "websiteURL": page_url,
                    "captchaId": captcha_id,
                    "version": 4,
                }
            else:
                task = {
                    "type": "GeetestTaskProxyLess",
                    "websiteURL": page_url,
                    "gt": gt,
                    "challenge": challenge,
                }
            result = self._capsolver_submit(task)
            # Capsolver returns dict for geetest, not just a token string
            return result  # handled specially below

        def _2cap():
            if version == 4:
                task_id = _2captcha_post({
                    "method": "geetest_v4",
                    "captcha_id": captcha_id,
                    "pageurl": page_url,
                }, self._2captcha_key)
            else:
                task_id = _2captcha_post({
                    "method": "geetest",
                    "gt": gt,
                    "challenge": challenge,
                    "pageurl": page_url,
                }, self._2captcha_key)
            return _2captcha_get_result(task_id, self._2captcha_key) if task_id else None

        def _anti():
            if version == 4:
                tid = _anticaptcha_post({
                    "type": "GeeTestTaskProxyless",
                    "websiteURL": page_url,
                    "version": 4,
                    "initParameters": {"captcha_id": captcha_id},
                }, self._anticaptcha_key)
            else:
                tid = _anticaptcha_post({
                    "type": "GeeTestTaskProxyless",
                    "websiteURL": page_url,
                    "gt": gt,
                    "challenge": challenge,
                }, self._anticaptcha_key)
            return _anticaptcha_get_result(tid, self._anticaptcha_key) if tid else None

        # Capsolver는 dict 반환, 나머지는 문자열 반환
        order = ["capsolver", "2captcha", "anticaptcha"]
        if self._preferred and self._preferred in order:
            order = [self._preferred] + [s for s in order if s != self._preferred]

        key_map = {
            "capsolver": self._capsolver_key,
            "2captcha": self._2captcha_key,
            "anticaptcha": self._anticaptcha_key,
        }
        fn_map = {"capsolver": _capsolver, "2captcha": _2cap, "anticaptcha": _anti}

        for svc in order:
            if not key_map.get(svc):
                continue
            try:
                res = fn_map[svc]()
                if res:
                    if isinstance(res, dict):
                        return res
                    # 문자열인 경우 파싱 시도 (2captcha/anticaptcha)
                    try:
                        return json.loads(res)
                    except Exception:
                        return {"geetest_validate": res}
                logger.warning(f"[CaptchaSolver] Geetest {svc} 실패, 폴백...")
            except Exception as e:
                logger.warning(f"[CaptchaSolver] Geetest {svc} 예외: {e}")
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

        def _capsolver():
            return self._capsolver_submit({
                "type": "ImageToTextTask",
                "body": image_b64,
                "case": case_sensitive,
            })

        def _ezcap():
            task_id = _ezcaptcha_post({
                "type": "ImageToTextTask",
                "body": image_b64,
                "case": case_sensitive,
            }, self._ezcaptcha_key)
            return _ezcaptcha_get_result(task_id, self._ezcaptcha_key) if task_id else None

        def _2cap():
            task_id = _2captcha_post({
                "method": "base64",
                "body": image_b64,
                "regsense": int(case_sensitive),
            }, self._2captcha_key)
            return _2captcha_get_result(task_id, self._2captcha_key) if task_id else None

        def _anti():
            task_id = _anticaptcha_post({
                "type": "ImageToTextTask",
                "body": image_b64,
                "case": case_sensitive,
            }, self._anticaptcha_key)
            return _anticaptcha_get_result(task_id, self._anticaptcha_key) if task_id else None

        return self._try_services({
            "capsolver": _capsolver, "ezcaptcha": _ezcap,
            "2captcha": _2cap, "anticaptcha": _anti,
        })

    # ──────────────────────────────────────────────────────────────────────
    # Playwright 통합 헬퍼
    # ──────────────────────────────────────────────────────────────────────

    def inject_recaptcha_token(self, page, token: str):
        """
        Playwright 페이지에 reCAPTCHA v2 토큰을 주입하고 콜백까지 실행한다.
        """
        safe_token = json.dumps(token)   # 따옴표/특수문자 escape
        page.evaluate(f"""
            (() => {{
                const el = document.getElementById('g-recaptcha-response');
                if (el) {{
                    el.value = {safe_token};
                    el.style.display = 'block';
                }}
                // reCAPTCHA 콜백 실행 (서버 측 검증 트리거)
                try {{
                    const cfg = window.___grecaptcha_cfg;
                    if (cfg && cfg.clients) {{
                        for (const id of Object.keys(cfg.clients)) {{
                            const c = cfg.clients[id];
                            const fn = c?.aa?.l?.callback || c?.l?.callback;
                            if (typeof fn === 'function') {{ fn({safe_token}); break; }}
                        }}
                    }}
                }} catch(e) {{}}
            }})();
        """)

    def inject_hcaptcha_token(self, page, token: str):
        """Playwright 페이지에 hCaptcha 토큰을 주입한다."""
        safe_token = json.dumps(token)
        page.evaluate(f"""
            (() => {{
                const el = document.querySelector('[name="h-captcha-response"]');
                if (el) el.value = {safe_token};
                // hCaptcha 콜백 실행
                try {{
                    if (window.hcaptcha) {{
                        const wid = Object.keys(hcaptcha._c || {{}})[0];
                        if (wid !== undefined) hcaptcha._c[wid].b.callback({safe_token});
                    }}
                }} catch(e) {{}}
            }})();
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

        # ── reCAPTCHA v2 감지
        if "g-recaptcha" in html or "grecaptcha" in html:
            site_key = self._extract_recaptcha_sitekey(page, html, version=2)
            if site_key:
                logger.info(f"[CaptchaSolver] reCAPTCHA v2 감지: {site_key[:20]}...")
                token = self.solve_recaptcha_v2(site_key, current_url)
                if token:
                    try:
                        self.inject_recaptcha_token(page, token)
                        return True
                    except Exception as e:
                        logger.warning(f"[CaptchaSolver] reCAPTCHA v2 주입 실패: {e}")

        # ── reCAPTCHA v3 감지 (score-based, 보이지 않는 CAPTCHA)
        if "grecaptcha.execute" in html or "recaptcha/api.js?render=" in html:
            site_key = self._extract_recaptcha_sitekey(page, html, version=3)
            if site_key:
                logger.info(f"[CaptchaSolver] reCAPTCHA v3 감지: {site_key[:20]}...")
                token = self.solve_recaptcha_v3(site_key, current_url)
                if token:
                    try:
                        page.evaluate(f"grecaptcha.execute({json.dumps(site_key)}).then(() => {{}})")
                        return True
                    except Exception as e:
                        logger.warning(f"[CaptchaSolver] reCAPTCHA v3 주입 실패: {e}")

        # ── hCaptcha 감지
        if "hcaptcha" in html.lower():
            site_key = self._extract_hcaptcha_sitekey(page, html)
            if site_key:
                logger.info(f"[CaptchaSolver] hCaptcha 감지: {site_key[:20]}...")
                token = self.solve_hcaptcha(site_key, current_url)
                if token:
                    try:
                        self.inject_hcaptcha_token(page, token)
                        return True
                    except Exception as e:
                        logger.warning(f"[CaptchaSolver] hCaptcha 주입 실패: {e}")

        # ── Cloudflare Turnstile 감지
        if "turnstile" in html.lower() or "cf-challenge" in html.lower():
            m = re.search(r'data-sitekey=["\']([0-9a-zA-Z_\-]{10,})["\']', html)
            if m:
                site_key = m.group(1)
                logger.info(f"[CaptchaSolver] Turnstile 감지: {site_key[:20]}...")
                token = self.solve_turnstile(site_key, current_url)
                if token:
                    try:
                        safe_token = json.dumps(token)
                        page.evaluate(f"""
                            (() => {{
                                const el = document.querySelector('[name="cf-turnstile-response"]');
                                if (el) el.value = {safe_token};
                                try {{ if (window.turnstile) turnstile.implicitRender(); }} catch(e) {{}}
                            }})();
                        """)
                        return True
                    except Exception as e:
                        logger.warning(f"[CaptchaSolver] Turnstile 주입 실패: {e}")

        # ── Geetest v3 감지
        if "initGeetest" in html or ("geetest" in html.lower() and "gt:" in html):
            gt_id, challenge = self._extract_geetest_v3(html)
            if gt_id and challenge:
                logger.info(f"[CaptchaSolver] Geetest v3 감지: gt={gt_id[:16]}...")
                result = self.solve_geetest(current_url, gt=gt_id, challenge=challenge)
                if result:
                    try:
                        page.evaluate(f"""
                            (() => {{
                                const r = {json.dumps(result)};
                                ['geetest_challenge','geetest_validate','geetest_seccode'].forEach(n => {{
                                    const el = document.querySelector('input[name="' + n + '"]');
                                    if (el) el.value = r[n] || '';
                                }});
                            }})();
                        """)
                        return True
                    except Exception as e:
                        logger.warning(f"[CaptchaSolver] Geetest v3 주입 실패: {e}")

        # ── Geetest v4 감지
        if "gt4" in html.lower() or ("geetest" in html.lower() and "captcha_id" in html):
            captcha_id = self._extract_geetest_v4(html)
            if captcha_id:
                logger.info(f"[CaptchaSolver] Geetest v4 감지: captchaId={captcha_id[:16]}...")
                result = self.solve_geetest(current_url, captcha_id=captcha_id, version=4)
                if result:
                    try:
                        page.evaluate(f"window.__gt4Result = {json.dumps(result)};")
                        return True
                    except Exception as e:
                        logger.warning(f"[CaptchaSolver] Geetest v4 주입 실패: {e}")

        return False

    # ──────────────────────────────────────────────────────────────────────
    # sitekey 추출 헬퍼 (HTML + JS DOM 동시 시도)
    # ──────────────────────────────────────────────────────────────────────

    def _extract_recaptcha_sitekey(self, page, html: str, version: int = 2) -> Optional[str]:
        """reCAPTCHA sitekey를 HTML 파싱 → JS DOM 순서로 추출."""
        # HTML에서 먼저 시도
        if version == 2:
            m = re.search(r'data-sitekey=["\']([^"\']{20,})["\']', html)
            if m and ("g-recaptcha" in html or "grecaptcha" in html):
                return m.group(1)
        else:
            m = re.search(r'render=([A-Za-z0-9_\-]{20,})', html)
            if m:
                return m.group(1)
            m = re.search(r'["\']sitekey["\']\s*:\s*["\']([A-Za-z0-9_\-]{20,})["\']', html)
            if m:
                return m.group(1)

        # JS DOM에서 추출
        try:
            key = page.evaluate("""() => {
                // data-sitekey 속성 직접 조회
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                // grecaptcha config에서 추출
                try {
                    const cfg = window.___grecaptcha_cfg;
                    if (cfg && cfg.clients) {
                        for (const id of Object.keys(cfg.clients)) {
                            const c = cfg.clients[id];
                            const sk = c?.l?.sitekey || c?.aa?.l?.sitekey;
                            if (sk) return sk;
                        }
                    }
                } catch(e) {}
                // script src에서 render= 파라미터
                const s = document.querySelector('script[src*="recaptcha/api.js"]');
                if (s) {
                    const m = s.src.match(/render=([^&]+)/);
                    if (m) return m[1];
                }
                return null;
            }""")
            if key and len(key) >= 20:
                return key
        except Exception:
            pass
        return None

    def _extract_hcaptcha_sitekey(self, page, html: str) -> Optional[str]:
        """hCaptcha sitekey를 HTML → JS DOM 순서로 추출."""
        m = re.search(r'data-sitekey=["\']([^"\']{20,})["\']', html)
        if m:
            return m.group(1)
        try:
            key = page.evaluate("""() => {
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                // hcaptcha config
                try {
                    if (window.hcaptcha) {
                        const wid = Object.keys(hcaptcha._c || {})[0];
                        if (wid !== undefined) return hcaptcha._c[wid]?.sitekey || null;
                    }
                } catch(e) {}
                return null;
            }""")
            if key and len(key) >= 20:
                return key
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_geetest_v3(html: str):
        """Geetest v3 gt + challenge 추출."""
        m_gt = re.search(r'["\']?gt["\']?\s*:\s*["\']([a-f0-9]{32})["\']', html)
        m_ch = re.search(r'["\']?challenge["\']?\s*:\s*["\']([a-f0-9]{32,})["\']', html)
        return (m_gt.group(1) if m_gt else None,
                m_ch.group(1) if m_ch else None)

    @staticmethod
    def _extract_geetest_v4(html: str) -> Optional[str]:
        """Geetest v4 captchaId 추출."""
        m = re.search(r'["\']?captcha_?[Ii]d["\']?\s*:\s*["\']([a-f0-9]{32})["\']', html)
        return m.group(1) if m else None

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


def reset_solver():
    """설정 변경 후 전역 인스턴스를 초기화한다 (다음 get_solver() 호출 시 재생성)."""
    global _SOLVER
    _SOLVER = None
    logger.info("[CaptchaSolver] 전역 인스턴스 초기화됨")
