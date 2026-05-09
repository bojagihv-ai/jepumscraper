"""
scrapers/coupang_scraper.py — 쿠팡 스크래퍼 (ProCrawler 8신호 통합)
────────────────────────────────────────────────────────────────────────────
우회 전략 (engine/pro_crawler.py 위임):
  1. IP 평판      : 프록시 로테이션 (ip_manager)
  2. 세션 나이     : 쿠키 예열 세션 재사용 (session_manager)
  3. 쿠키 이력     : 도메인별 쿠키 축적 (session_manager + bypass_engine)
  4. TLS/HTTP2    : Chrome 124 JA3/JA4 + H2 SETTINGS (fingerprint_suite)
  5. 행동 패턴     : 베지어 마우스 + 관성 스크롤 + 읽기 정지 (human_behavior)
  6. 요청 속도     : 토큰 버킷 + 적응형 백오프 (rate_limiter)
  7. 탐색 경로     : 메인 → 검색 경로 시뮬레이션 (navigation)
  8. 지역 신호     : 한국 로케일 + KR 쿠키 (regional)
"""

import asyncio
import contextvars
import hashlib
import logging
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import List
from urllib.parse import quote_plus, unquote_plus

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from engines.text_matcher import extract_keywords
from scrapers.base_scraper import BaseScraper, ProductResult, download_image_sync

logger = logging.getLogger(__name__)

LOG_DIR = os.path.join(str(config.BASE_DIR), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

COUPANG_HOME_URL = "https://www.coupang.com/"
COUPANG_SEARCH_SELECTORS = (
    'input[name="q"]',
    '#headerSearchKeyword',
    'input[placeholder*="상품"]',
    'input[type="search"]',
    'input[type="text"]',
)


class PlatformBlocked(RuntimeError):
    pass


# ─── 유틸 ────────────────────────────────────────────────────────────────────

def _safe_log_name(prefix: str, query: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', query).strip(' ._')
    cleaned = re.sub(r'\s+', '_', cleaned)[:40] or 'query'
    digest  = hashlib.sha1(query.encode('utf-8', errors='ignore')).hexdigest()[:8]
    return f'{prefix}_{cleaned}_{digest}.html'


def _build_queries(keyword: str) -> List[str]:
    queries = [keyword]
    kws     = extract_keywords(keyword)
    if kws:
        q2 = ' '.join(kws)
        if q2 != keyword:
            queries.append(q2)
        core = [w for w in kws if len(w) >= 3]
        if core:
            q3 = ' '.join(core[:4])
            if q3 != q2:
                queries.append(q3)
    return queries


def _looks_blocked(html: str) -> bool:
    if not html:
        return False
    lowered = html.lower()
    return (
        'access denied' in lowered
        or 'you don\'t have permission to access' in lowered
        or 'captcha' in lowered
        or 'verify you are human' in lowered
    )


# ─── HTML 파싱 ────────────────────────────────────────────────────────────────

def _coupang_debug_port() -> int:
    return int(getattr(config, "COUPANG_DEBUG_PORT", 9223) or 9223)


def _find_autohotkey_exe() -> str:
    configured = getattr(config, "AUTOHOTKEY_EXE", "") or os.getenv("AUTOHOTKEY_EXE", "")
    candidates = [configured] if configured else []
    candidates.extend([
        r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe",
        r"C:\Program Files\AutoHotkey\v2\AutoHotkey.exe",
        r"C:\Program Files\AutoHotkey\AutoHotkey64.exe",
        r"C:\Program Files\AutoHotkey\AutoHotkey.exe",
        r"C:\Program Files (x86)\AutoHotkey\AutoHotkey.exe",
    ])
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def _open_coupang_debug_home() -> bool:
    if not getattr(config, "COUPANG_ASSISTED_CAPTURE", True):
        return False
    if _has_coupang_debug_page():
        return True

    try:
        from engine.browser_profile import (
            chrome_browser_path,
            coupang_browser_profile_dir,
            coupang_import_extension_dir,
        )
        chrome = chrome_browser_path()
        if not chrome:
            logger.warning("[Coupang] Chrome executable not found")
            return False

        profile_dir = coupang_browser_profile_dir()
        extension_dir = coupang_import_extension_dir()
        args = [
            chrome,
            f"--user-data-dir={profile_dir}",
            "--profile-directory=Default",
            f"--remote-debugging-port={_coupang_debug_port()}",
            "--remote-allow-origins=*",
        ]
        if (extension_dir / "manifest.json").is_file():
            args.extend([
                f"--disable-extensions-except={extension_dir}",
                f"--load-extension={extension_dir}",
            ])
        args.append(COUPANG_HOME_URL)
        subprocess.Popen(args, close_fds=True)
    except Exception as exc:
        logger.warning("[Coupang] failed to open assisted Chrome: %s", exc)
        return False

    for _ in range(24):
        time.sleep(0.5)
        if _has_coupang_debug_page():
            return True
    return False


def _activate_coupang_window() -> bool:
    try:
        import win32con
        import win32gui

        matches = []

        def _cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetClassName(hwnd) != "Chrome_WidgetWin_1":
                return
            title = win32gui.GetWindowText(hwnd) or ""
            lowered = title.lower()
            if "coupang" in lowered or "쿠팡" in title:
                matches.append(hwnd)

        win32gui.EnumWindows(_cb, None)
        if not matches:
            return False
        hwnd = matches[0]
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        return True
    except Exception as exc:
        logger.debug("[Coupang] window activation skipped: %s", exc)
        return False


def _focus_coupang_search_input(page) -> bool:
    for selector in COUPANG_SEARCH_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.scroll_into_view_if_needed(timeout=1500)
            locator.click(timeout=3000)
            return True
        except Exception:
            try:
                focused = page.evaluate(
                    """selector => {
                        const el = document.querySelector(selector);
                        if (!el) return false;
                        el.focus();
                        if (typeof el.select === 'function') el.select();
                        return true;
                    }""",
                    selector,
                )
                if focused:
                    return True
            except Exception:
                continue
    return False


def _safe_page_content(page, retries: int = 6, delay: float = 0.7) -> str:
    """Read page HTML while tolerating short navigation windows."""
    for _ in range(max(1, retries)):
        try:
            return page.content()
        except Exception as exc:
            message = str(exc).lower()
            if "navigating" not in message and "changing the content" not in message:
                logger.debug("[Coupang] page.content skipped: %s", exc)
                break
            time.sleep(delay)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2500)
            except Exception:
                pass
    return ""


def _run_coupang_form_search(page, query: str) -> bool:
    for selector in COUPANG_SEARCH_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.fill(query, timeout=3000)
            time.sleep(0.15)
            locator.press("Enter", timeout=3000)
            logger.info("[Coupang] submitted search via browser form")
            return True
        except Exception as exc:
            logger.debug("[Coupang] form search failed selector=%s error=%s", selector, exc)

    try:
        submitted = page.evaluate(
            """query => {
                const selectors = [
                    'input[name="q"]',
                    '#headerSearchKeyword',
                    'input[placeholder*="상품"]',
                    'input[type="search"]',
                    'input[type="text"]'
                ];
                for (const selector of selectors) {
                    const el = document.querySelector(selector);
                    if (!el) continue;
                    el.focus();
                    el.value = query;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    const form = el.closest('form');
                    if (form) {
                        form.requestSubmit ? form.requestSubmit() : form.submit();
                        return true;
                    }
                    el.dispatchEvent(new KeyboardEvent('keydown', {
                        key: 'Enter',
                        code: 'Enter',
                        keyCode: 13,
                        which: 13,
                        bubbles: true
                    }));
                    return true;
                }
                return false;
            }""",
            query,
        )
        if submitted:
            logger.info("[Coupang] submitted search via DOM form")
            return True
    except Exception as exc:
        logger.debug("[Coupang] DOM form search failed: %s", exc)
    return False


def _set_clipboard_text(text: str):
    try:
        import win32clipboard
        import win32con

        old_text = ""
        had_text = False
        win32clipboard.OpenClipboard()
        try:
            try:
                old_text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                had_text = True
            except Exception:
                old_text = ""
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        return had_text, old_text
    except Exception as exc:
        logger.debug("[Coupang] clipboard set failed: %s", exc)
        return None


def _restore_clipboard_text(snapshot) -> None:
    if snapshot is None:
        return
    try:
        import win32clipboard
        import win32con

        had_text, old_text = snapshot
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            if had_text:
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, old_text)
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        logger.debug("[Coupang] clipboard restore failed: %s", exc)


def _run_coupang_ahk_search(query: str) -> bool:
    if not getattr(config, "ENABLE_AHK_FALLBACK", True):
        return False
    exe = _find_autohotkey_exe()
    if not exe:
        return False

    tools_dir = Path(config.BASE_DIR) / "tools"
    v1_script = tools_dir / "coupang_search_v1.ahk"
    v2_script = tools_dir / "coupang_search_v2.ahk"
    scripts = [v2_script, v1_script] if "\\v2\\" in exe.lower() else [v1_script, v2_script]
    for script in scripts:
        if not script.exists():
            continue
        try:
            completed = subprocess.run(
                [exe, str(script), query],
                timeout=12,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                logger.info("[Coupang] submitted search via AHK: %s", script.name)
                return True
            logger.debug(
                "[Coupang] AHK search failed script=%s rc=%s stderr=%s",
                script.name,
                completed.returncode,
                completed.stderr,
            )
        except Exception as exc:
            logger.debug("[Coupang] AHK search failed script=%s error=%s", script.name, exc)
    return False


def _run_coupang_pyautogui_search(query: str) -> bool:
    try:
        import pyautogui
    except Exception as exc:
        logger.debug("[Coupang] pyautogui unavailable: %s", exc)
        return False

    snapshot = _set_clipboard_text(query)
    if snapshot is None:
        return False
    try:
        pyautogui.PAUSE = 0.08
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)
        pyautogui.press("enter")
        logger.info("[Coupang] submitted search via pyautogui")
        return True
    except Exception as exc:
        logger.debug("[Coupang] pyautogui search failed: %s", exc)
        return False
    finally:
        time.sleep(0.35)
        _restore_clipboard_text(snapshot)


def _run_coupang_ui_search(query: str) -> str:
    if not query.strip():
        return "unavailable"
    if not _open_coupang_debug_home():
        return "unavailable"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "unavailable"

    debug_port = _coupang_debug_port()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            page = None
            for context in browser.contexts:
                for candidate in context.pages:
                    if "coupang.com" in (candidate.url or ""):
                        page = candidate
                        break
                if page:
                    break
            if not page:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                page.goto(COUPANG_HOME_URL, wait_until="domcontentloaded", timeout=20000)

            page.bring_to_front()
            _activate_coupang_window()
            if "coupang.com" not in (page.url or "") or "/np/search" in (page.url or ""):
                page.goto(COUPANG_HOME_URL, wait_until="domcontentloaded", timeout=20000)
                time.sleep(random.uniform(1.0, 1.8))

            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass

            html = _safe_page_content(page, retries=4, delay=0.5)
            if _looks_blocked(html):
                browser.close()
                return "blocked"

            if not _focus_coupang_search_input(page):
                browser.close()
                return "unavailable"

            submitted = _run_coupang_ahk_search(query)
            if not submitted:
                submitted = _run_coupang_form_search(page, query)
            if not submitted:
                submitted = _run_coupang_pyautogui_search(query)
            if not submitted:
                browser.close()
                return "unavailable"

            try:
                page.wait_for_url("**/np/search**", timeout=15000)
            except Exception:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
            time.sleep(random.uniform(2.0, 3.2))

            current_url = page.url or ""
            if "/np/search" in current_url:
                browser.close()
                return "submitted"
            if "/np/search" not in current_url and _run_coupang_form_search(page, query):
                try:
                    page.wait_for_url("**/np/search**", timeout=15000)
                except Exception:
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                time.sleep(random.uniform(2.0, 3.2))
                current_url = page.url or ""
                if "/np/search" in current_url:
                    browser.close()
                    return "submitted"

            html = _safe_page_content(page, retries=8, delay=0.8)
            if html and _looks_blocked(html):
                browser.close()
                return "blocked"
            current_url = page.url or ""
            if "/np/search" not in current_url and html:
                try:
                    if _parse_coupang_html(html, 1):
                        browser.close()
                        return "submitted"
                except Exception:
                    pass
            browser.close()
            return "submitted" if "/np/search" in current_url else "unavailable"
    except Exception as exc:
        logger.info("[Coupang] UI search unavailable: %s", exc)
        return "unavailable"


def _parse_coupang_html(html: str, max_count: int) -> List[ProductResult]:
    soup    = BeautifulSoup(html, 'html.parser')
    results: List[ProductResult] = []

    selector_strategies = [
        'li[class*="ProductUnit_productUnit"]',
        'li[class*="productUnit"]',
        'li[class*="search-product"]',
        'li.search-product',
        'div[class*="ProductCard"]',
        'li[class*="ProductCard"]',
        'article[class*="product"]',
        'div[class*="searchResult"] li',
    ]

    items = []
    for sel in selector_strategies:
        try:
            found = soup.select(sel)
            if found:
                items = found
                logger.debug(f"[쿠팡] 셀렉터 '{sel}' → {len(found)}개")
                break
        except Exception:
            continue

    if not items:
        for a in soup.find_all('a', href=True):
            href = a.get('href', '')
            if '/vp/products/' in href or '/products/' in href:
                container = (
                    a.find_parent('li') or a.find_parent('article') or a.find_parent('div')
                )
                if container and container not in items:
                    items.append(container)
            if len(items) >= max_count:
                break

    for item in items:
        if len(results) >= max_count:
            break
        try:
            texts = list(item.stripped_strings)
            if not texts:
                continue

            if texts[-1].upper() in ('AD', '광고', 'ADVERTISEMENT'):
                continue

            title = texts[0][:120]
            if len(title) < 3:
                continue

            price = 0
            for t in texts[1:]:
                if '원' in t and any(c.isdigit() for c in t):
                    nums = re.sub(r'[^0-9]', '', t)
                    if nums:
                        try:
                            price = int(nums); break
                        except Exception:
                            pass
            if price == 0:
                m = re.search(r'(\d{3},\d{3})', item.get_text())
                if m:
                    try: price = int(m.group(1).replace(',', ''))
                    except Exception: pass

            img_el    = item.select_one('img[src], img[data-src]')
            if not img_el:
                continue
            thumb_url = img_el.get('src') or img_el.get('data-src') or ''
            if thumb_url.startswith('//'):
                thumb_url = 'https:' + thumb_url
            if not thumb_url or 'data:image' in thumb_url:
                continue

            item_link = ''
            for a in item.find_all('a', href=True):
                href = a.get('href', '')
                if '/vp/products/' in href or '/products/' in href:
                    item_link = href; break
            if not item_link:
                a_tag = item.select_one('a[href]')
                if a_tag:
                    item_link = a_tag.get('href', '')
            if not item_link:
                continue
            if item_link.startswith('/'):
                item_link = 'https://www.coupang.com' + item_link

            prod_id = f"coupang_{random.randint(100000, 999999)}"
            m = re.search(r'products/(\d+)', item_link)
            if m:
                prod_id = f"coupang_{m.group(1)}"

            results.append(ProductResult(
                id=prod_id, platform="쿠팡", title=title,
                price=str(price), product_url=item_link, thumbnail_url=thumb_url,
                seller_name="쿠팡",
            ))
        except Exception as e:
            logger.debug(f"[쿠팡] 파싱 오류: {e}")

    return results


# ─── ProCrawler 기반 검색 ────────────────────────────────────────────────────

def _scrape_coupang_assisted_current_page(query: str, max_count: int) -> List[ProductResult]:
    """Read the currently open, user-driven Coupang search tab via Chrome CDP.

    This mode is intentionally user-assisted: the user opens/logs in/searches in
    the dedicated Chrome profile, and the program only parses the page that is
    already visible to the user.
    """
    if not getattr(config, "COUPANG_ASSISTED_CAPTURE", True):
        return []
    debug_port = int(getattr(config, "COUPANG_DEBUG_PORT", 9223) or 9223)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    query_norm = re.sub(r"\s+", "", query).lower()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            pages = []
            for context in browser.contexts:
                pages.extend(context.pages)
            for page in pages:
                url = page.url or ""
                if "coupang.com" not in url or "/np/search" not in url:
                    continue
                decoded_url = unquote_plus(url)
                if query_norm and query_norm not in re.sub(r"\s+", "", decoded_url).lower():
                    continue
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                for attempt in range(5):
                    if attempt:
                        try:
                            page.wait_for_load_state("networkidle", timeout=3500)
                        except Exception:
                            pass
                        try:
                            page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.75)); true")
                        except Exception:
                            pass
                        time.sleep(0.8 + attempt * 0.25)
                    html = _safe_page_content(page, retries=8, delay=0.75)
                    if not html:
                        logger.info("[쿠팡] assisted current page still navigating; retry %d", attempt + 1)
                        continue
                    if _looks_blocked(html):
                        logger.warning("[쿠팡] assisted current page is blocked")
                        break
                    results = _parse_coupang_html(html, max_count)
                    logger.info("[쿠팡] assisted current page → %d개 (try %d)", len(results), attempt + 1)
                    if results:
                        browser.close()
                        return results
            browser.close()
    except Exception as exc:
        logger.info("[쿠팡] assisted capture unavailable: %s", exc)
    return []


def _has_coupang_debug_page() -> bool:
    if not getattr(config, "COUPANG_ASSISTED_CAPTURE", True):
        return False
    debug_port = int(getattr(config, "COUPANG_DEBUG_PORT", 9223) or 9223)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            for context in browser.contexts:
                for page in context.pages:
                    if "coupang.com" in (page.url or ""):
                        browser.close()
                        return True
            browser.close()
    except Exception:
        return False
    return False


def _scrape_coupang_pro(query: str, max_count: int) -> List[ProductResult]:
    """
    ProCrawler로 쿠팡 검색 (8신호 통합).
    1순위: SmartSession (TLS+쿠키)
    2순위: Playwright (JS 렌더링)
    """
    search_url = (
        f'https://www.coupang.com/np/search'
        f'?q={quote_plus(query)}&channel=user&sorter=bestAsc'
    )

    try:
        from engine.pro_crawler import get_crawler
        crawler = get_crawler()
    except ImportError:
        return _scrape_coupang_playwright_fallback(query, max_count)

    logger.info(f'[쿠팡] ProCrawler 검색: "{query}"')

    # requests 시도
    resp = crawler.fetch(
        search_url,
        referer='https://www.coupang.com/',
        platform='coupang',
    )

    if resp and resp.ok:
        if not _looks_blocked(resp.text):
            results = _parse_coupang_html(resp.text, max_count)
            logger.info(f'[쿠팡] SmartSession → {len(results)}개')
            if results:
                return results
        else:
            logger.warning('[쿠팡] SmartSession 차단 응답 감지')

    # Playwright 폴백
    logger.info(f'[쿠팡] Playwright 폴백: "{query}"')
    html = crawler.fetch_playwright(
        search_url,
        platform='coupang',
        referer='https://www.coupang.com/',
    )
    if not html:
        return []

    if _looks_blocked(html):
        dump = os.path.join(LOG_DIR, _safe_log_name('coupang_blocked', query))
        with open(dump, 'w', encoding='utf-8') as f:
            f.write(html)
        raise PlatformBlocked('blocked: Coupang Access Denied')

    results = _parse_coupang_html(html, max_count)
    logger.info(f'[쿠팡] Playwright → {len(results)}개')

    if not results:
        dump = os.path.join(LOG_DIR, _safe_log_name('coupang_fail', query))
        with open(dump, 'w', encoding='utf-8') as f:
            f.write(html)

    return results


# ─── 레거시 Playwright 폴백 (ProCrawler import 실패 시) ───────────────────────

def _scrape_coupang_playwright_fallback(query: str, max_count: int) -> List[ProductResult]:
    """ProCrawler 없이 Playwright만으로 검색 (구형 폴백)."""
    try:
        from playwright.sync_api import sync_playwright
        from engine.stealth import get_full_stealth_script
    except ImportError as e:
        logger.warning(f'[쿠팡] Playwright 미설치: {e}')
        return []

    search_url = (
        f'https://www.coupang.com/np/search'
        f'?q={quote_plus(query)}&channel=user&sorter=bestAsc'
    )
    logger.info(f'[쿠팡] 레거시 Playwright 폴백: "{query}"')
    results = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=['--no-sandbox','--disable-dev-shm-usage',
                      '--disable-blink-features=AutomationControlled',
                      '--lang=ko-KR','--window-size=1920,1080'],
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                locale='ko-KR', timezone_id='Asia/Seoul',
            )
            page = context.new_page()
            page.add_init_script(get_full_stealth_script())

            try:
                page.goto('https://www.coupang.com', wait_until='domcontentloaded', timeout=20000)
            except Exception: pass
            time.sleep(random.uniform(2.0, 3.5))
            for _ in range(random.randint(2, 4)):
                page.mouse.wheel(0, random.randint(200, 500))
                time.sleep(random.uniform(0.4, 0.8))
            time.sleep(random.uniform(1.0, 2.0))

            try:
                page.goto(search_url, wait_until='domcontentloaded', timeout=30000)
            except Exception as eg:
                logger.warning(f'[쿠팡] goto 실패: {eg}')
                browser.close()
                return []

            time.sleep(random.uniform(3.0, 4.5))
            html = page.content()
            if 'Access Denied' in html or len(html) < 2000:
                time.sleep(5)
                try:
                    page.goto(search_url, wait_until='domcontentloaded', timeout=25000)
                    time.sleep(3.0)
                    html = page.content()
                except Exception: pass

            if _looks_blocked(html):
                browser.close()
                raise PlatformBlocked('blocked: Coupang Access Denied')

            for _ in range(6):
                page.mouse.wheel(0, random.randint(300, 600))
                time.sleep(random.uniform(0.3, 0.6))
            html = page.content()
            browser.close()

            results = _parse_coupang_html(html, max_count)
            logger.info(f'[쿠팡] 레거시 Playwright → {len(results)}개')

    except PlatformBlocked:
        raise
    except Exception as e:
        logger.error(f'[쿠팡] 레거시 Playwright 오류: {e}')

    return results


# ─── 스크래퍼 클래스 ─────────────────────────────────────────────────────────

class CoupangScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.platform_name = '쿠팡'

    def _scrape_sync(self, keyword: str) -> List[ProductResult]:
        queries = _build_queries(keyword)
        logger.info(f'[쿠팡] 쿼리 목록: {queries}')

        seen_ids:    set           = set()
        all_results: List[ProductResult] = []

        for query in queries:
            if len(all_results) >= config.MAX_CANDIDATES:
                break

            results = _scrape_coupang_assisted_current_page(query, config.MAX_CANDIDATES)
            if not results and getattr(config, "COUPANG_ASSISTED_CAPTURE", True):
                ui_status = _run_coupang_ui_search(query)
                logger.info("[Coupang] UI search status=%s query=%s", ui_status, query)
                if ui_status == "submitted":
                    results = _scrape_coupang_assisted_current_page(
                        query,
                        config.MAX_CANDIDATES,
                    )
                elif ui_status == "blocked":
                    self.last_status = "blocked"
                    self.last_error = "blocked: Coupang Access Denied"
                    return []
                else:
                    self.last_status = "manual_required"
                    self.last_error = (
                        "Coupang assisted Chrome search could not be submitted. "
                        "Open the dedicated Coupang Chrome window, confirm the page is usable, "
                        "then run the search again."
                    )
                    return []
            if not results and not getattr(config, "COUPANG_ASSISTED_CAPTURE", True):
                results = _scrape_coupang_pro(query, config.MAX_CANDIDATES)

            for r in results:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    all_results.append(r)
                    if len(all_results) >= config.MAX_CANDIDATES:
                        break

            if len(all_results) >= 5:
                break

        logger.info(f'[쿠팡] 최종 {len(all_results)}개 수집')
        return all_results

    async def search(self, keyword: str) -> List[ProductResult]:
        loop    = asyncio.get_running_loop()
        self.last_status = ""
        self.last_error = ""
        try:
            ctx = contextvars.copy_context()
            results = await loop.run_in_executor(None, ctx.run, self._scrape_sync, keyword)
        except PlatformBlocked as e:
            self.last_status = "blocked"
            self.last_error = str(e)
            raise

        for res in results:
            if res.thumbnail_url:
                local_path = os.path.join(str(config.THUMBNAIL_DIR), f'{res.id}.jpg')
                ok = await loop.run_in_executor(
                    None, download_image_sync, res.thumbnail_url, local_path
                )
                if ok:
                    res.local_thumbnail_path = local_path

        if results:
            self.last_status = "success"
        elif not self.last_status:
            self.last_status = "zero_result"
        return results
