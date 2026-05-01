from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse, urlunparse

import config

logger = logging.getLogger(__name__)

STATE_PATH = Path(config.DATA_DIR) / "proxy_profiles_state.json"
DEFAULT_HEALTH_URL = "https://api.ipify.org?format=json"

PLATFORM_POLICIES = {
    "naver": {"mode": "direct_api"},
    "elevenst": {"mode": "direct_preferred"},
    "gmarket": {"mode": "direct_preferred"},
    "coupang": {"mode": "cautious", "blocked_cooldown_sec": 1800},
    "auction": {"mode": "cautious", "blocked_cooldown_sec": 1800},
}

_ACTIVE_SELECTION: contextvars.ContextVar["ProxySelection | None"] = contextvars.ContextVar(
    "jepum_proxy_selection",
    default=None,
)


@dataclass
class ProxyProfile:
    id: str
    name: str
    proxy_url: str
    enabled: bool = True
    proxy_type: str = "unknown"
    country: str = "KR"
    allowed_platforms: List[str] = field(default_factory=list)
    chrome_user_data_dir: str = ""
    chrome_profile_directory: str = ""

    attempts: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    total_duration_ms: int = 0
    last_status: str = ""
    last_error: str = ""
    last_ip: str = ""
    last_checked_at: str = ""
    last_used_at: float = 0.0
    cooldown_until: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / self.attempts if self.attempts else 0.0

    @property
    def cooldown_active(self) -> bool:
        return time.time() < self.cooldown_until

    @property
    def host(self) -> str:
        return urlparse(self.proxy_url).hostname or ""

    def allows(self, platform: str) -> bool:
        if not self.allowed_platforms:
            return True
        return normalize_platform(platform) in {normalize_platform(p) for p in self.allowed_platforms}

    def score(self, platform: str) -> float:
        type_score = {
            "residential": 25.0,
            "isp": 20.0,
            "mobile": 18.0,
            "datacenter": 6.0,
            "unknown": 12.0,
        }.get(self.proxy_type, 12.0)
        platform_bonus = 8.0 if self.allows(platform) else -100.0
        success_bonus = min(35.0, self.success_rate * 35.0)
        duration_penalty = min(15.0, self.avg_duration_ms / 2000.0)
        failure_penalty = min(35.0, self.consecutive_failures * 12.0 + self.failures * 1.5)
        cooldown_penalty = 1000.0 if self.cooldown_active else 0.0
        return type_score + platform_bonus + success_bonus - duration_penalty - failure_penalty - cooldown_penalty

    def public_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["proxy_url"] = mask_proxy_url(self.proxy_url)
        data["success_rate"] = round(self.success_rate, 3)
        data["avg_duration_ms"] = round(self.avg_duration_ms, 1)
        data["cooldown_active"] = self.cooldown_active
        data["cooldown_until_epoch"] = self.cooldown_until
        return data


@dataclass
class ProxySelection:
    profile: Optional[ProxyProfile]
    platform: str = ""
    stage: str = "search"

    @property
    def proxy_url(self) -> str:
        return self.profile.proxy_url if self.profile else ""

    @property
    def profile_id(self) -> str:
        return self.profile.id if self.profile else "direct"

    @property
    def mode(self) -> str:
        return "proxy" if self.profile else "direct"

    def metadata(self) -> Dict[str, Any]:
        return {
            "proxy_mode": self.mode,
            "proxy_profile_id": self.profile_id,
            "proxy_name": self.profile.name if self.profile else "direct",
            "proxy_host": self.profile.host if self.profile else "",
        }


def normalize_platform(value: str = "") -> str:
    text = (value or "").lower()
    if "coupang" in text or "쿠팡" in text:
        return "coupang"
    if "auction" in text or "옥션" in text:
        return "auction"
    if "gmarket" in text or "g마켓" in text:
        return "gmarket"
    if "11st" in text or "11번가" in text or "eleven" in text:
        return "elevenst"
    if "naver" in text or "네이버" in text or "smartstore" in text:
        return "naver"
    return text.strip() or "unknown"


def mask_proxy_url(proxy_url: str) -> str:
    if not proxy_url:
        return ""
    parsed = urlparse(proxy_url)
    netloc = parsed.netloc
    if "@" in netloc:
        auth, host = netloc.rsplit("@", 1)
        username = auth.split(":", 1)[0]
        netloc = f"{username}:***@{host}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def playwright_proxy(proxy_url: str) -> Optional[Dict[str, str]]:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return None
    data = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        data["username"] = parsed.username
    if parsed.password:
        data["password"] = parsed.password
    return data


def _profile_id(proxy_url: str, name: str = "") -> str:
    digest = hashlib.sha1(proxy_url.encode("utf-8", errors="ignore")).hexdigest()[:10]
    prefix = "".join(ch for ch in (name or "proxy").lower() if ch.isalnum())[:16] or "proxy"
    return f"{prefix}_{digest}"


def _infer_type(value: str) -> str:
    lower = value.lower()
    if "residential" in lower or "resi" in lower:
        return "residential"
    if "mobile" in lower:
        return "mobile"
    if "isp" in lower:
        return "isp"
    if "datacenter" in lower or "dc" in lower:
        return "datacenter"
    return "unknown"


class ProxyManager:
    def __init__(self):
        self._lock = Lock()
        self._profiles: Dict[str, ProxyProfile] = {}
        self._load_profiles()
        self._load_state()

    def select(self, platform: str = "", stage: str = "search") -> ProxySelection:
        platform_key = normalize_platform(platform)
        if not bool(getattr(config, "ENABLE_PROXY_PROFILES", False)):
            return ProxySelection(None, platform_key, stage)

        policy = PLATFORM_POLICIES.get(platform_key, {})
        if policy.get("mode") == "direct_api":
            return ProxySelection(None, platform_key, stage)

        with self._lock:
            candidates = [
                p for p in self._profiles.values()
                if p.enabled and p.proxy_url and p.allows(platform_key) and not p.cooldown_active
            ]
            if not candidates:
                logger.info("[ProxyManager] usable proxy profile not found for %s; direct mode", platform_key)
                return ProxySelection(None, platform_key, stage)

            chosen = max(candidates, key=lambda p: p.score(platform_key))
            chosen.last_used_at = time.time()
            self._save_state_locked()
            logger.info("[ProxyManager] %s uses profile %s (%s)", platform_key, chosen.name, chosen.host)
            return ProxySelection(chosen, platform_key, stage)

    def record_result(
        self,
        selection: ProxySelection | None,
        *,
        platform: str = "",
        status: str = "",
        success: bool = False,
        duration_ms: int = 0,
        error: str = "",
        public_ip: str = "",
    ) -> None:
        if not selection or not selection.profile:
            return
        platform_key = normalize_platform(platform or selection.platform)
        with self._lock:
            profile = self._profiles.get(selection.profile.id)
            if not profile:
                return
            profile.attempts += 1
            profile.total_duration_ms += int(duration_ms or 0)
            profile.last_status = status
            profile.last_error = error
            if public_ip:
                profile.last_ip = public_ip
            if success:
                profile.successes += 1
                profile.consecutive_failures = 0
                profile.cooldown_until = 0.0
            else:
                profile.failures += 1
                profile.consecutive_failures += 1
                if status in {"blocked", "captcha", "throttled", "login_required", "timeout"}:
                    cooldown = PLATFORM_POLICIES.get(platform_key, {}).get("blocked_cooldown_sec", 900)
                    profile.cooldown_until = max(profile.cooldown_until, time.time() + float(cooldown))
            self._save_state_locked()

    def health_check(self, profile_id: str = "") -> Dict[str, Any]:
        profiles: Iterable[ProxyProfile]
        with self._lock:
            profiles = [self._profiles[profile_id]] if profile_id and profile_id in self._profiles else list(self._profiles.values())
        results = []
        for profile in profiles:
            results.append(self._check_one(profile))
        return {"ok": all(r.get("ok") for r in results) if results else True, "results": results}

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            profiles = [p.public_dict() for p in self._profiles.values()]
        return {
            "enabled": bool(getattr(config, "ENABLE_PROXY_PROFILES", False)),
            "profile_count": len(profiles),
            "profiles": profiles,
            "policies": PLATFORM_POLICIES,
            "health_check_url": getattr(config, "PROXY_HEALTH_CHECK_URL", DEFAULT_HEALTH_URL),
        }

    def _check_one(self, profile: ProxyProfile) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            import requests
            response = requests.get(
                getattr(config, "PROXY_HEALTH_CHECK_URL", DEFAULT_HEALTH_URL),
                proxies={"http": profile.proxy_url, "https": profile.proxy_url},
                timeout=float(getattr(config, "PROXY_HEALTH_TIMEOUT_SEC", 8)),
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            response.raise_for_status()
            payload = response.json() if "json" in response.headers.get("content-type", "") else {}
            public_ip = payload.get("ip") or response.text.strip()[:80]
            self.record_result(
                ProxySelection(profile, "health", "health_check"),
                platform="health",
                status="health_ok",
                success=True,
                duration_ms=duration_ms,
                public_ip=public_ip,
            )
            profile.last_checked_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            return {
                "ok": True,
                "profile_id": profile.id,
                "name": profile.name,
                "public_ip": public_ip,
                "duration_ms": duration_ms,
            }
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self.record_result(
                ProxySelection(profile, "health", "health_check"),
                platform="health",
                status="health_error",
                success=False,
                duration_ms=duration_ms,
                error=str(exc),
            )
            return {
                "ok": False,
                "profile_id": profile.id,
                "name": profile.name,
                "error": str(exc),
                "duration_ms": duration_ms,
            }

    def _load_profiles(self) -> None:
        raw_profiles = list(getattr(config, "PROXY_PROFILES", []) or [])
        env_list = os.environ.get("PROXY_LIST", "")
        if env_list:
            for item in env_list.split(","):
                proxy_url = item.strip()
                if proxy_url:
                    raw_profiles.append({"name": urlparse(proxy_url).hostname or "env_proxy", "proxy_url": proxy_url})

        env_file = os.environ.get("PROXY_FILE", "")
        if env_file and Path(env_file).exists():
            for line in Path(env_file).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                raw_profiles.append({
                    "name": urlparse(parts[0]).hostname or "file_proxy",
                    "proxy_url": parts[0],
                    "proxy_type": parts[1] if len(parts) > 1 else _infer_type(parts[0]),
                })

        for raw in raw_profiles:
            proxy_url = str(raw.get("proxy_url") or raw.get("url") or "").strip()
            if not proxy_url:
                continue
            name = str(raw.get("name") or urlparse(proxy_url).hostname or "proxy")
            profile = ProxyProfile(
                id=str(raw.get("id") or _profile_id(proxy_url, name)),
                name=name,
                proxy_url=proxy_url,
                enabled=bool(raw.get("enabled", True)),
                proxy_type=str(raw.get("proxy_type") or _infer_type(f"{name} {proxy_url}")),
                country=str(raw.get("country") or "KR"),
                allowed_platforms=[normalize_platform(p) for p in raw.get("allowed_platforms", [])],
                chrome_user_data_dir=str(raw.get("chrome_user_data_dir") or ""),
                chrome_profile_directory=str(raw.get("chrome_profile_directory") or ""),
            )
            self._profiles[profile.id] = profile

        if self._profiles:
            logger.info("[ProxyManager] %d proxy profile(s) loaded", len(self._profiles))
        else:
            logger.info("[ProxyManager] no proxy profile; direct mode")

    def _load_state(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("[ProxyManager] state load failed: %s", exc)
            return
        for profile_id, saved in state.items():
            profile = self._profiles.get(profile_id)
            if not profile:
                continue
            for key in (
                "attempts", "successes", "failures", "consecutive_failures",
                "total_duration_ms", "last_status", "last_error", "last_ip",
                "last_checked_at", "last_used_at", "cooldown_until",
            ):
                if key in saved:
                    setattr(profile, key, saved[key])

    def _save_state_locked(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        state = {
            profile_id: {
                "attempts": p.attempts,
                "successes": p.successes,
                "failures": p.failures,
                "consecutive_failures": p.consecutive_failures,
                "total_duration_ms": p.total_duration_ms,
                "last_status": p.last_status,
                "last_error": p.last_error,
                "last_ip": p.last_ip,
                "last_checked_at": p.last_checked_at,
                "last_used_at": p.last_used_at,
                "cooldown_until": p.cooldown_until,
            }
            for profile_id, p in self._profiles.items()
        }
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


_MANAGER: Optional[ProxyManager] = None


def get_manager() -> ProxyManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ProxyManager()
    return _MANAGER


def reload_manager() -> ProxyManager:
    global _MANAGER
    _MANAGER = ProxyManager()
    return _MANAGER


@contextlib.contextmanager
def use_proxy_for(platform: str, stage: str = "search"):
    selection = get_manager().select(platform, stage)
    token = _ACTIVE_SELECTION.set(selection)
    try:
        yield selection
    finally:
        _ACTIVE_SELECTION.reset(token)


def get_active_selection() -> ProxySelection:
    return _ACTIVE_SELECTION.get() or ProxySelection(None)


def get_proxy(platform: str = "") -> Optional[str]:
    active = _ACTIVE_SELECTION.get()
    if active is not None:
        return active.proxy_url or None
    selection = get_manager().select(platform or "unknown")
    return selection.proxy_url or None


def get_playwright_proxy(platform: str = "") -> Optional[Dict[str, str]]:
    return playwright_proxy(get_proxy(platform) or "")


def record_active_result(
    *,
    platform: str = "",
    status: str = "",
    success: bool = False,
    duration_ms: int = 0,
    error: str = "",
) -> None:
    get_manager().record_result(
        get_active_selection(),
        platform=platform,
        status=status,
        success=success,
        duration_ms=duration_ms,
        error=error,
    )
