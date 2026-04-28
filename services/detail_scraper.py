"""
services/detail_scraper.py - 상세페이지 캡처 (ProScraper 스텔스 강화)
──────────────────────────────────────────────────────────────────────
3단계 폴백 전략:
  네이버:    화면 캡처 (로그인 세션 활용)
  쿠팡/기타: 1) Playwright + ProScraper 스텔스  →  2) DrissionPage

ProScraper 스텔스 통합:
  - fingerprint.py + advanced_fingerprint.py 통합 스크립트 주입
  - 쿠팡: 메인 페이지 방문 후 상세 페이지 (Akamai 세션 확립)
"""
import asyncio
import glob
import logging
import os
import random
import shutil
import sys
import tempfile
import time
from typing import Dict, List

# 상위 디렉토리 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# 쿠팡 '더보기' 버튼 클릭 JS
EXPAND_JS = """
(function() {
    const keywords = ['상품정보 더보기', '상세정보 더보기', '상품상세 더보기'];
    const allBtns = Array.from(document.querySelectorAll('button, a'));
    for (const kw of keywords) {
        for (const btn of allBtns) {
            const txt = (btn.innerText || btn.textContent || '').trim();
            if (txt === kw) {
                btn.scrollIntoView({behavior: 'instant', block: 'center'});
                btn.click();
                return 'clicked: ' + txt;
            }
        }
    }
    for (const btn of allBtns) {
        const txt = (btn.innerText || btn.textContent || '').trim();
        if (txt.includes('정보 더보기')) {
            btn.scrollIntoView({behavior: 'instant', block: 'center'});
            btn.click();
            return 'clicked (partial): ' + txt;
        }
    }
    return 'not found';
})();
"""

BLOCKED_KEYWORDS = [
    "Access Denied",
    "쇼핑 서비스 접속이 일시적으로 제한",
    "비정상적인 접근",
    "자동화된 요청",
    "로봇이 아닙니다",
    "captcha",
    "보안 확인을 완료해 주세요",
    "영수증 번호를 입력",
    "상품을 찾을 수 없습니다",
    "판매가 중지된 상품",
    "페이지를 찾을 수 없습니다",
]


def _get_drission_page():
    from DrissionPage import ChromiumOptions, ChromiumPage
    co = ChromiumOptions()
    co.set_argument("--window-position=0,0")
    co.set_argument("--window-size=1920,1080")
    co.set_argument("--mute-audio")
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--lang=ko-KR")
    co.set_argument("--force-color-profile=srgb")
    co.set_argument("--force-device-scale-factor=1")
    co.set_argument("--run-all-compositor-stages-before-draw")

    paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for path in paths:
        if os.path.exists(path):
            co.set_paths(browser_path=path)
            break

    return ChromiumPage(co)


def _is_blocked(html: str) -> bool:
    lower = html.lower()
    return any(kw.lower() in lower for kw in BLOCKED_KEYWORDS) or len(html) < 1000


def _copy_chrome_profile_tmp(chrome_profile: str) -> str:
    if not os.path.exists(chrome_profile):
        return ""
    try:
        tmp_dir = tempfile.mkdtemp(prefix='pw_chrome_')
        default_src = os.path.join(chrome_profile, 'Default')
        default_dst = os.path.join(tmp_dir, 'Default')
        os.makedirs(default_dst, exist_ok=True)
        for fname in ['Cookies', 'Local Storage', 'Session Storage', 'Preferences']:
            src = os.path.join(default_src, fname)
            dst = os.path.join(default_dst, fname)
            try:
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
                elif os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
            except Exception:
                pass
        return tmp_dir
    except Exception as e:
        logger.warning(f"Chrome 프로필 복사 실패: {e}")
        return ""


class DetailScraper:
    def __init__(self):
        self.output_dir = config.DETAIL_DIR

    # ──────────────────────────────────────────────────────────────────────
    #  【1순위】 DrissionPage 캡처 (쿠팡·기타)
    # ──────────────────────────────────────────────────────────────────────
    def _capture_drission(self, product_url: str, product_id: str,
                           output_dir: str, slice_height: int = 0) -> Dict:
        result = {"screenshots": [], "mhtml_path": ""}
        os.makedirs(output_dir, exist_ok=True)

        page = _get_drission_page()
        try:
            is_coupang = "coupang.com" in product_url

            if is_coupang:
                logger.info(f"[{product_id}] 쿠팡 메인 선방문...")
                page.get("https://www.coupang.com", retry=1, interval=1, timeout=30)
                time.sleep(1.5)

            logger.info(f"[{product_id}] 상세 페이지 이동: {product_url}")
            page.get(product_url, retry=1, interval=1, timeout=45)
            time.sleep(3.5)

            html = page.html
            if _is_blocked(html):
                logger.warning(f"[{product_id}] DrissionPage 봇 차단 감지")
                page.quit()
                return result

            for _ in range(15):
                page.run_js("window.scrollBy(0, window.innerHeight * 0.8)")
                time.sleep(0.3)
            page.run_js("window.scrollTo(0, 0)")
            time.sleep(1)

            if is_coupang:
                self._expand_coupang_details(page, product_id)

            body_height = page.run_js("return document.body.scrollHeight") or 1080
            fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
            raw_png = os.path.join(output_dir, f"{product_id}_fullpage.png")

            page.run_js("window.scrollTo(0, 0)")
            time.sleep(2.0)

            captured = False
            try:
                page.get_screenshot(path=output_dir, name=f"{product_id}_fullpage.png", full_page=True)
                if os.path.exists(raw_png):
                    img = Image.open(raw_png)
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    brightness = np.array(img).mean()
                    if brightness < 15:
                        img.close(); os.remove(raw_png)
                    else:
                        img.save(fullpage_path, "JPEG", quality=85)
                        img.close(); os.remove(raw_png)
                        captured = True
            except Exception as e:
                logger.warning(f"[{product_id}] DrissionPage 캡처 실패: {e}")

            if not captured:
                fullpage_path = self._scroll_capture_fallback(page, output_dir, product_id, int(body_height))

            page.quit()

            if not fullpage_path or not os.path.exists(fullpage_path):
                return result

            if slice_height > 0:
                result["screenshots"] = self._slice_image(fullpage_path, output_dir, slice_height)
            else:
                result["screenshots"] = [fullpage_path]

            return result

        except Exception as e:
            logger.error(f"[{product_id}] DrissionPage 오류: {e}")
            try: page.quit()
            except Exception: pass
            return result

    def _expand_coupang_details(self, page, product_id: str) -> None:
        clicked = False
        for kw in ["상품정보 더보기", "상세정보 더보기"]:
            try:
                btn = page.ele(f"text:{kw}", timeout=2)
                if btn:
                    btn.click()
                    clicked = True
                    break
            except Exception:
                pass

        if not clicked:
            result = page.run_js(EXPAND_JS)
            time.sleep(1.5)
            old_h = page.run_js("return document.body.scrollHeight") or 0
            time.sleep(2)
            new_h = page.run_js("return document.body.scrollHeight") or 0
            if int(new_h) > int(old_h) + 100:
                clicked = True

        if clicked:
            time.sleep(2)
            for _ in range(10):
                page.run_js("window.scrollBy(0, window.innerHeight)")
                time.sleep(0.4)
            time.sleep(1)
            body_h = page.run_js("return document.body.scrollHeight") or 10000
            steps = max(30, int(body_h / 300))
            for i in range(steps):
                y = int(i * body_h / steps)
                page.run_js(f"window.scrollTo(0, {y})")
                time.sleep(0.2)
            page.run_js("window.scrollTo(0, 0)")
            time.sleep(2)

    # ──────────────────────────────────────────────────────────────────────
    #  【2순위】 Playwright + ProScraper 스텔스 캡처 (쿠팡·기타 1순위)
    # ──────────────────────────────────────────────────────────────────────
    def _capture_playwright(self, product_url: str, product_id: str,
                             output_dir: str, is_naver: bool = False) -> Dict:
        """
        Playwright + ProScraper 완전 스텔스 캡처.
        쿠팡: 메인 페이지 먼저 방문 (Akamai 세션 확립) 후 상세 페이지.
        """
        result = {"screenshots": [], "mhtml_path": ""}
        os.makedirs(output_dir, exist_ok=True)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("[Playwright] 미설치 → 차선책 불가")
            return result

        try:
            from engine.stealth import get_full_stealth_script
            stealth_script = get_full_stealth_script()
        except ImportError:
            # 폴백: 기본 스텔스만
            stealth_script = """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR','ko','en-US']});
            """

        is_coupang = "coupang.com" in product_url
        logger.info(f"[{product_id}] Playwright 스텔스 캡처 시작: {product_url}")

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--lang=ko-KR",
            "--window-size=1920,1080",
            "--force-device-scale-factor=1",
            "--disable-web-security",
        ]

        try:
            with sync_playwright() as pw:
                context = None
                tmp_profile = None

                # 네이버: Chrome 프로필 임시복사로 로그인 세션 활용
                if is_naver:
                    chrome_profile = os.path.join(
                        os.environ.get('LOCALAPPDATA', ''),
                        r"Google\Chrome\User Data"
                    )
                    tmp_profile = _copy_chrome_profile_tmp(chrome_profile)
                    if tmp_profile:
                        try:
                            context = pw.chromium.launch_persistent_context(
                                user_data_dir=tmp_profile,
                                channel="chrome",
                                headless=False,
                                args=launch_args,
                                viewport={"width": 1920, "height": 1080},
                                no_viewport=False,
                            )
                        except Exception as e:
                            logger.warning(f"[{product_id}] 프로필 컨텍스트 실패: {e}")
                            context = None

                # 프로필 없으면 일반 브라우저
                if context is None:
                    browser = pw.chromium.launch(headless=True, args=launch_args)
                    context = browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        locale="ko-KR",
                        timezone_id="Asia/Seoul",
                        extra_http_headers={
                            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
                            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
                            "sec-ch-ua-mobile": "?0",
                            "sec-ch-ua-platform": '"Windows"',
                        },
                    )

                page = context.new_page()

                # ★ ProScraper 풀 스텔스 주입
                page.add_init_script(stealth_script)

                # 쿠팡: 메인 페이지 먼저 방문 (Akamai 세션 확립)
                if is_coupang:
                    logger.info(f"[{product_id}] 쿠팡 메인 방문 (Akamai 세션 확립)...")
                    try:
                        page.goto("https://www.coupang.com", wait_until="domcontentloaded", timeout=20000)
                        time.sleep(random.uniform(2.0, 3.5))
                        # 자연스러운 스크롤
                        for _ in range(random.randint(2, 3)):
                            page.mouse.wheel(0, random.randint(200, 400))
                            time.sleep(random.uniform(0.3, 0.7))
                        time.sleep(1.0)
                    except Exception:
                        pass

                # 상세 페이지 이동
                try:
                    page.goto(product_url, wait_until="domcontentloaded", timeout=35000)
                except Exception as eg:
                    logger.warning(f"[{product_id}] goto 실패: {eg}")

                time.sleep(random.uniform(3.0, 4.5))

                content = page.content()
                if _is_blocked(content):
                    logger.warning(f"[{product_id}] Playwright 봇 차단 감지")
                    context.close()
                    if tmp_profile:
                        shutil.rmtree(tmp_profile, ignore_errors=True)
                    return result

                # lazy-load 스크롤 (1차)
                body_h = page.evaluate("document.body.scrollHeight") or 5000
                steps = max(20, int(body_h / 400))
                for i in range(steps):
                    y = int(i * body_h / steps)
                    page.evaluate(f"window.scrollTo(0, {y})")
                    time.sleep(0.15)
                time.sleep(1)

                # 쿠팡: 더보기 클릭
                if is_coupang:
                    for kw in ["상품정보 더보기", "상세정보 더보기", "상품상세 더보기"]:
                        try:
                            btn = page.locator(f"text={kw}").first
                            if btn.count() > 0 and btn.is_visible(timeout=2000):
                                btn.scroll_into_view_if_needed()
                                btn.click()
                                logger.info(f"[{product_id}] 더보기 클릭: '{kw}'")
                                time.sleep(2)
                                break
                        except Exception:
                            pass

                # 최종 느린 스크롤 (lazy-load 이미지 완전 로드)
                final_h = page.evaluate("document.body.scrollHeight") or body_h
                slow_steps = max(30, min(80, int(final_h / 250)))
                for i in range(slow_steps):
                    y = int(i * final_h / slow_steps)
                    page.evaluate(f"window.scrollTo(0, {y})")
                    time.sleep(0.2)
                page.evaluate(f"window.scrollTo(0, {final_h})")
                time.sleep(2.5)
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(1.5)

                # 풀페이지 스크린샷
                png_path = os.path.join(output_dir, f"{product_id}_pw.png")
                page.screenshot(path=png_path, full_page=True)
                context.close()
                if tmp_profile:
                    shutil.rmtree(tmp_profile, ignore_errors=True)

                if not os.path.exists(png_path):
                    return result

                img = Image.open(png_path)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                brightness = np.array(img).mean()
                if brightness < 15:
                    logger.warning(f"[{product_id}] 검은 이미지 (밝기={brightness:.1f})")
                    img.close(); os.remove(png_path)
                    return result

                fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
                img.save(fullpage_path, "JPEG", quality=85)
                img.close(); os.remove(png_path)

                logger.info(f"[{product_id}] Playwright 스텔스 캡처 완료: 밝기={brightness:.1f}")
                result["screenshots"] = [fullpage_path]
                return result

        except Exception as e:
            logger.error(f"[{product_id}] Playwright 오류: {e}")
            if tmp_profile:
                shutil.rmtree(tmp_profile, ignore_errors=True)
            return result

    # ──────────────────────────────────────────────────────────────────────
    #  【3순위】 화면 캡처 (네이버 전용, 최후 수단)
    # ──────────────────────────────────────────────────────────────────────
    def _capture_naver_via_screen(self, product_url: str, product_id: str,
                                   output_dir: str) -> Dict:
        """
        네이버 화면 캡처 v2.
        Chrome subprocess 열기 → PrintWindow API 캡처 → 이어붙이기.
        """
        import subprocess
        import ctypes

        result = {"screenshots": [], "mhtml_path": ""}
        os.makedirs(output_dir, exist_ok=True)

        chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        if not os.path.exists(chrome_exe):
            logger.error("[화면캡처] Chrome 없음")
            return result

        logger.info(f"[{product_id}] 화면 캡처 v2 시작: {product_url}")

        hwnd_target = None
        tab_opened = False
        new_window_opened = False
        NAVER_HINTS = ['naver', '스마트스토어', 'smartstore', '네이버']

        def _get_chrome_wins():
            wins = []
            def _cb(hwnd, _):
                if (win32gui.IsWindowVisible(hwnd)
                        and win32gui.GetClassName(hwnd) == 'Chrome_WidgetWin_1'):
                    t = win32gui.GetWindowText(hwnd)
                    if t:
                        wins.append((hwnd, t))
            win32gui.EnumWindows(_cb, None)
            return wins

        try:
            import win32gui, win32con, win32api

            chrome_before = _get_chrome_wins()
            if chrome_before:
                subprocess.Popen([chrome_exe, product_url])
                tab_opened = True
            else:
                subprocess.Popen([chrome_exe, '--new-window', product_url])
                new_window_opened = True

            time.sleep(4)
            for _wait in range(24):
                for hwnd, title in _get_chrome_wins():
                    if any(hint in title.lower() for hint in NAVER_HINTS):
                        hwnd_target = hwnd
                        break
                if hwnd_target:
                    break
                time.sleep(0.5)

            if not hwnd_target:
                wins_now = _get_chrome_wins()
                if wins_now:
                    hwnd_target = wins_now[0][0]

            if not hwnd_target:
                logger.error(f"[{product_id}] Chrome 창 확보 실패")
                return result

            page_title = win32gui.GetWindowText(hwnd_target)
            CAPTCHA_TITLES = ["보안 확인", "Security Check", "로봇이 아닙니다"]
            if any(cw in page_title for cw in CAPTCHA_TITLES):
                logger.warning(f"[{product_id}] CAPTCHA 감지 → 포기")
                return result

            win32gui.ShowWindow(hwnd_target, win32con.SW_MAXIMIZE)
            time.sleep(0.3)
            try:
                win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.05)
                ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                time.sleep(0.5)
            except Exception as _fe:
                try:
                    ctypes.windll.user32.SwitchToThisWindow(hwnd_target, True)
                    time.sleep(0.3)
                except Exception:
                    pass

            time.sleep(1.5)

        except Exception as e:
            logger.error(f"[{product_id}] Chrome 초기화 실패: {e}")
            return result

        try:
            def _print_window_capture(hwnd) -> Image.Image:
                import win32gui as _wg, win32ui, win32con as _wc, ctypes as _ct
                rect = _wg.GetWindowRect(hwnd)
                w = rect[2] - rect[0]; h = rect[3] - rect[1]
                if w <= 0 or h <= 0:
                    raise ValueError(f"Invalid size: {w}x{h}")
                hwnd_dc = _wg.GetWindowDC(hwnd)
                mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
                save_dc = mfc_dc.CreateCompatibleDC()
                bmp = win32ui.CreateBitmap()
                bmp.CreateCompatibleBitmap(mfc_dc, w, h)
                save_dc.SelectObject(bmp)
                ok = _ct.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
                if not ok:
                    save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), _wc.SRCCOPY)
                bmp_info = bmp.GetInfo()
                bmp_bits = bmp.GetBitmapBits(True)
                img = Image.frombuffer('RGB',
                    (bmp_info['bmWidth'], bmp_info['bmHeight']),
                    bmp_bits, 'raw', 'BGRX', 0, 1)
                save_dc.DeleteDC(); mfc_dc.DeleteDC()
                _wg.ReleaseDC(hwnd, hwnd_dc)
                _wg.DeleteObject(bmp.GetHandle())
                return img

            rect = win32gui.GetWindowRect(hwnd_target)
            win_w = rect[2] - rect[0]
            win_h = rect[3] - rect[1]
            CHROME_UI = 130

            render_hwnd = None
            def _find_renderer(child_hwnd, _):
                nonlocal render_hwnd
                if win32gui.GetClassName(child_hwnd) == 'Chrome_RenderWidgetHostHWND':
                    render_hwnd = child_hwnd
            try:
                win32gui.EnumChildWindows(hwnd_target, _find_renderer, None)
            except Exception:
                pass

            VK_HOME  = 0x24
            HOME_DN  = 1 | (0x47 << 16) | (1 << 24)
            HOME_UP  = HOME_DN | (1 << 30) | (1 << 31)
            if render_hwnd:
                try:
                    win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_HOME, HOME_DN)
                    time.sleep(0.05)
                    win32api.SendMessage(render_hwnd, win32con.WM_KEYUP,   VK_HOME, HOME_UP)
                    time.sleep(0.5)
                except Exception:
                    pass

            VK_SPACE = 0x20
            SPACE_DN = 1 | (0x39 << 16)
            SPACE_UP = SPACE_DN | (1 << 30) | (1 << 31)
            SCROLL_COUNT = 12
            screenshots = []

            for _si in range(SCROLL_COUNT + 1):
                try:
                    full_img = _print_window_capture(hwnd_target)
                    content = full_img.crop((0, CHROME_UI, win_w, win_h))
                    screenshots.append(content)
                    full_img.close()
                except Exception as e:
                    logger.warning(f"[{product_id}] 캡처 {_si} 실패: {e}")

                if _si < SCROLL_COUNT:
                    if render_hwnd:
                        try:
                            win32api.SendMessage(render_hwnd, win32con.WM_KEYDOWN, VK_SPACE, SPACE_DN)
                            time.sleep(0.05)
                            win32api.SendMessage(render_hwnd, win32con.WM_KEYUP,   VK_SPACE, SPACE_UP)
                        except Exception:
                            pass
                    else:
                        import pyautogui as _pag
                        _pag.press('space')
                    time.sleep(0.7)

            if not screenshots:
                return result

            w_px = screenshots[0].width
            merged = Image.new("RGB", (w_px, sum(im.height for im in screenshots)))
            y_off = 0
            for im in screenshots:
                merged.paste(im.convert("RGB"), (0, y_off))
                y_off += im.height

            brightness = np.array(merged).mean()
            if brightness < 15:
                merged.close()
                return result

            fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
            merged.save(fullpage_path, "JPEG", quality=85)
            merged.close()
            logger.info(f"[{product_id}] 화면 캡처 완료: 밝기={brightness:.1f}")
            result["screenshots"] = [fullpage_path]
            return result

        except Exception as e:
            logger.error(f"[{product_id}] 화면 캡처 오류: {e}")
            return result

        finally:
            try:
                if hwnd_target:
                    try:
                        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                        time.sleep(0.05)
                        ctypes.windll.user32.SetForegroundWindow(hwnd_target)
                        time.sleep(0.3)
                    except Exception:
                        pass
                    if new_window_opened:
                        win32gui.PostMessage(hwnd_target, win32con.WM_CLOSE, 0, 0)
                    elif tab_opened:
                        import pyautogui as _pag
                        _pag.hotkey('ctrl', 'w')
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────
    #  메인 진입점: 3단계 폴백 전략
    # ──────────────────────────────────────────────────────────────────────
    def _capture_sync(self, product_url: str, product_id: str,
                       slice_height: int = 0) -> Dict:
        """
        폴백 전략으로 캡처.
          네이버:    화면 캡처 (로그인 세션 활용)
          쿠팡/기타: 1) Playwright 스텔스  →  2) DrissionPage
        """
        product_detail_dir = os.path.join(self.output_dir, str(product_id))
        os.makedirs(product_detail_dir, exist_ok=True)

        is_naver = "naver.com" in product_url or "smartstore" in product_url

        if is_naver:
            logger.info(f"[{product_id}] 네이버: 화면 캡처 실행...")
            result = self._capture_naver_via_screen(product_url, product_id, product_detail_dir)
            return self._apply_slice(result, product_detail_dir, slice_height)
        else:
            # 쿠팡·기타: Playwright 스텔스 1순위
            logger.info(f"[{product_id}] Playwright 스텔스 1순위 시도...")
            result = self._capture_playwright(product_url, product_id, product_detail_dir, is_naver=False)
            if result.get("screenshots"):
                return self._apply_slice(result, product_detail_dir, slice_height)

            logger.info(f"[{product_id}] Playwright 실패 → DrissionPage 차선책...")
            result = self._capture_drission(product_url, product_id, product_detail_dir, slice_height)
            return result

    def _apply_slice(self, result: Dict, output_dir: str, slice_height: int) -> Dict:
        if not result.get("screenshots") or slice_height <= 0:
            return result
        new_shots = []
        for p in result["screenshots"]:
            if os.path.exists(p):
                chunks = self._slice_image(p, output_dir, slice_height)
                new_shots.extend(chunks)
        result["screenshots"] = new_shots
        return result

    # ──────────────────────────────────────────────────────────────────────
    #  유틸리티
    # ──────────────────────────────────────────────────────────────────────
    def _scroll_capture_fallback(self, page, output_dir: str,
                                  product_id: str, body_height: int) -> str:
        viewport_h = page.run_js("return window.innerHeight") or 1080
        num_chunks = max(1, min((body_height // viewport_h) + 1, 30))

        chunk_imgs = []
        page.run_js("window.scrollTo(0, 0)")
        time.sleep(0.5)

        for i in range(num_chunks):
            y = i * viewport_h
            page.run_js(f"window.scrollTo(0, {y})")
            time.sleep(0.4)
            try:
                chunk_name = f"_chunk_{i:03d}.png"
                page.get_screenshot(path=output_dir, name=chunk_name)
                chunk_path = os.path.join(output_dir, chunk_name)
                if os.path.exists(chunk_path):
                    chunk_imgs.append(chunk_path)
            except Exception as e:
                logger.warning(f"Chunk {i} 캡처 실패: {e}")

        if not chunk_imgs:
            return ""

        pil_imgs = [Image.open(p).convert("RGB") for p in chunk_imgs]
        total_h = sum(im.height for im in pil_imgs)
        merged = Image.new("RGB", (pil_imgs[0].width, total_h))
        y_offset = 0
        for im in pil_imgs:
            merged.paste(im, (0, y_offset))
            y_offset += im.height
        for im in pil_imgs:
            im.close()

        fullpage_path = os.path.join(output_dir, f"{product_id}_fullpage.jpg")
        merged.save(fullpage_path, "JPEG", quality=85)
        merged.close()

        for p in chunk_imgs:
            try: os.remove(p)
            except Exception: pass

        return fullpage_path

    def _slice_image(self, fullpage_path: str, output_dir: str,
                      slice_height: int) -> List[str]:
        for old in glob.glob(os.path.join(output_dir, "part_*.jpg")):
            try: os.remove(old)
            except Exception: pass

        img = Image.open(fullpage_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        img_arr = np.array(img)
        row_variance = np.std(img_arr, axis=(1, 2))

        search_range = 150
        current_y = 0
        part_idx = 1
        chunks = []

        while current_y < h:
            target_y = current_y + slice_height
            if target_y >= h:
                crop = img.crop((0, current_y, w, h))
                part_path = os.path.join(output_dir, f"part_{part_idx:03d}.jpg")
                crop.save(part_path, "JPEG", quality=85)
                chunks.append(part_path)
                break

            search_start = max(current_y + 50, target_y - search_range)
            search_end = min(h - 1, target_y + search_range)
            sub = row_variance[search_start:search_end]
            best_y = search_start + int(np.argmin(sub)) if len(sub) > 0 else target_y

            crop = img.crop((0, current_y, w, best_y))
            part_path = os.path.join(output_dir, f"part_{part_idx:03d}.jpg")
            crop.save(part_path, "JPEG", quality=85)
            chunks.append(part_path)

            current_y = best_y
            part_idx += 1

        img.close()
        return chunks

    async def capture_detail_page(
        self, product_url: str, product_id: str, slice_height: int = 0
    ) -> Dict:
        """비동기 래퍼."""
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, self._capture_sync, product_url, product_id, slice_height
        )
        return result
