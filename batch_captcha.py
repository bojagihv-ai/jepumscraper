import win32gui, subprocess, os, time

log = r'C:\JepumScraper\logs\ahk_debug.log'

captcha_windows = []
def callback(hwnd, extra):
    if win32gui.IsWindowVisible(hwnd):
        title = win32gui.GetWindowText(hwnd)
        if 'Chrome' in title and ('잠시' in title or '기다리' in title):
            captcha_windows.append((hwnd, title))
win32gui.EnumWindows(callback, None)

print(f'Found {len(captcha_windows)} captcha windows')
for hwnd, title in captcha_windows:
    print(f'Processing: {hwnd}')
    result = subprocess.run([
        r'C:\Users\kua\AppData\Local\Programs\AutoHotkey\v2\AutoHotkey64.exe',
        r'C:\JepumScraper\tools\cf_captcha_click_v2.ahk',
        str(hwnd), '15', '400'
    ], capture_output=True, text=True, timeout=20)
    print(f'  Exit code: {result.returncode}')
    time.sleep(3)
    try:
        new_title = win32gui.GetWindowText(hwnd)
        if '잠시' not in new_title and '기다리' not in new_title:
            print(f'  RESOLVED! -> {new_title[:60]}')
        else:
            print(f'  Still captcha -> {new_title[:60]}')
    except:
        print('  Window closed')
