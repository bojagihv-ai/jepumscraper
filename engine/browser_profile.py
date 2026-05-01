from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def _active_proxy_profile():
    try:
        from engine.ip_manager import get_active_selection
        selection = get_active_selection()
        return selection.profile
    except Exception:
        return None


def chrome_profile_name(profile_directory: str | None = None) -> str:
    if profile_directory:
        return str(profile_directory)
    active = _active_proxy_profile()
    if active and active.chrome_profile_directory:
        return str(active.chrome_profile_directory)
    return str(getattr(config, "CHROME_PROFILE_DIRECTORY", "Default") or "Default")


def _profile_cookie_score(profile_path: Path, domain: str) -> tuple[int, int, list[str]]:
    cookie_db = profile_path / "Network" / "Cookies"
    if not cookie_db.exists():
        cookie_db = profile_path / "Cookies"
    if not cookie_db.exists():
        return 0, 0, []

    names: list[str] = []
    try:
        uri = cookie_db.as_uri() + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=1) as conn:
            where = "host_key LIKE ?"
            pattern = f"%{domain}%"
            total = int(conn.execute(f"SELECT COUNT(*) FROM cookies WHERE {where}", (pattern,)).fetchone()[0])
            names = [
                str(row[0])
                for row in conn.execute(
                    f"SELECT DISTINCT name FROM cookies WHERE {where} ORDER BY name LIMIT 80",
                    (pattern,),
                ).fetchall()
            ]
    except Exception as exc:
        logger.debug("[BrowserProfile] cookie scan skipped for %s: %s", profile_path.name, exc)
        return 0, 0, []

    auth_names = {"NID_AUT", "NID_SES", "NID_SAUTO", "NID_JST", "NAC", "NNB", "BUC"}
    auth_hits = sum(1 for name in names if name in auth_names)
    return total, auth_hits, names


def chrome_profiles_for_domain(domain: str, limit: int = 4) -> list[str]:
    """Return likely Chrome profile directories for a site, newest useful first."""
    configured = str(getattr(config, "NAVER_CHROME_PROFILE_DIRECTORY", "") or "").strip() if "naver" in domain else ""
    user_data_dir = chrome_user_data_dir()
    candidates: list[tuple[float, str, int, int]] = []

    if configured and (user_data_dir / configured).exists():
        candidates.append((10_000.0, configured, 0, 0))
    elif "naver" in domain:
        default_profile = chrome_profile_name()
        if (user_data_dir / default_profile).exists():
            candidates.append((9_000.0, default_profile, 0, 0))

    for profile_path in user_data_dir.iterdir() if user_data_dir.exists() else []:
        if not profile_path.is_dir():
            continue
        if profile_path.name != "Default" and not profile_path.name.startswith("Profile"):
            continue
        if any(profile_path.name == existing[1] for existing in candidates):
            continue
        total, auth_hits, _names = _profile_cookie_score(profile_path, domain)
        if total <= 0 and auth_hits <= 0:
            continue
        age_days = max(0.0, (time.time() - profile_path.stat().st_mtime) / 86400)
        recency_bonus = 40.0 if age_days <= 7 else 20.0 if age_days <= 30 else 0.0
        score = (auth_hits * 30.0) + min(total, 30) + recency_bonus
        candidates.append((score, profile_path.name, total, auth_hits))

    if not candidates:
        return [chrome_profile_name()]

    candidates.sort(key=lambda item: item[0], reverse=True)
    profiles: list[str] = []
    for _score, profile, total, auth_hits in candidates:
        if profile not in profiles:
            logger.info(
                "[BrowserProfile] candidate for %s: %s (cookies=%s auth_names=%s)",
                domain,
                profile,
                total,
                auth_hits,
            )
            profiles.append(profile)
        if len(profiles) >= max(1, limit):
            break
    fallback = chrome_profile_name()
    if fallback not in profiles:
        profiles.append(fallback)
    return profiles


def naver_chrome_profile_name() -> str:
    return chrome_profiles_for_domain("naver.com", limit=1)[0]


def chrome_user_data_dir(user_data_dir: str | os.PathLike | None = None) -> Path:
    if user_data_dir:
        return Path(user_data_dir)
    active = _active_proxy_profile()
    if active and active.chrome_user_data_dir:
        return Path(active.chrome_user_data_dir)
    configured = getattr(config, "CHROME_USER_DATA_DIR", "")
    if configured:
        return Path(configured)
    return Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def chrome_browser_path() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


def coupang_browser_profile_dir() -> Path:
    configured = getattr(config, "COUPANG_BROWSER_PROFILE_DIR", "")
    path = Path(configured) if configured else Path(config.DATA_DIR) / "browser_profiles" / "coupang"
    path.mkdir(parents=True, exist_ok=True)
    return path


def auction_browser_profile_dir() -> Path:
    configured = getattr(config, "AUCTION_BROWSER_PROFILE_DIR", "")
    path = Path(configured) if configured else Path(config.DATA_DIR) / "browser_profiles" / "auction"
    path.mkdir(parents=True, exist_ok=True)
    return path


def coupang_import_extension_dir() -> Path:
    path = Path(config.DATA_DIR) / "coupang_import_extension"
    path.mkdir(parents=True, exist_ok=True)
    return path


def use_user_browser_session() -> bool:
    return bool(getattr(config, "USE_USER_BROWSER_SESSION", True))


def copy_chrome_profile_tmp(
    source_user_data_dir: str | os.PathLike | None = None,
    profile_directory: str | None = None,
    allow_blank_for_proxy: bool = True,
) -> str:
    """Copy the active Chrome profile into a temporary user-data-dir.

    Playwright/DrissionPage can then use real cookies and local storage without
    locking or mutating the user's live Chrome profile.
    """
    if not use_user_browser_session():
        return ""

    user_data_dir = chrome_user_data_dir(source_user_data_dir)
    profile = chrome_profile_name(profile_directory)
    source_profile = user_data_dir / profile
    if not source_profile.exists():
        active = _active_proxy_profile()
        if allow_blank_for_proxy and active and active.proxy_url:
            tmp_root = Path(tempfile.mkdtemp(prefix=f"jepum_proxy_profile_{active.id}_"))
            (tmp_root / profile).mkdir(parents=True, exist_ok=True)
            logger.warning(
                "[BrowserProfile] profile for proxy %s not found; using blank temp profile",
                active.name,
            )
            return str(tmp_root)
        logger.warning("[BrowserProfile] Chrome profile not found: %s", source_profile)
        return ""

    tmp_root = Path(tempfile.mkdtemp(prefix="jepum_chrome_profile_"))
    tmp_profile = tmp_root / profile
    tmp_profile.mkdir(parents=True, exist_ok=True)

    def copy_path(relative: str) -> None:
        src = source_profile / relative
        dst = tmp_profile / relative
        try:
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except Exception as exc:
            logger.debug("[BrowserProfile] skipped %s: %s", src, exc)

    for relative in [
        "Cookies",
        "Cookies-journal",
        "Network/Cookies",
        "Network/Cookies-journal",
        "Local Storage",
        "Session Storage",
        "Preferences",
        "Secure Preferences",
    ]:
        copy_path(relative)

    try:
        local_state = user_data_dir / "Local State"
        if local_state.is_file():
            shutil.copy2(local_state, tmp_root / "Local State")
    except Exception as exc:
        logger.debug("[BrowserProfile] skipped Local State: %s", exc)

    return str(tmp_root)
