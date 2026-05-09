import time, os, win32gui

log_file = r'C:\JepumScraper\logs\server.log'
ahk_log = r'C:\JepumScraper\logs\ahk_debug.log'
success_log = r'C:\JepumScraper\logs\captcha_success.log'

with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
    f.seek(0, 2)
    server_pos = f.tell()

ahk_pos = 0
if os.path.exists(ahk_log):
    ahk_pos = os.path.getsize(ahk_log)

print("=" * 60)
print("  CAPTCHA REAL-TIME MONITOR")
print("  Started:", time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 60)
print()

start = time.time()
check_count = 0

while time.time() - start < 300:  # 5 minutes
    check_count += 1

    # 1. Server log
    with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
        f.seek(server_pos)
        new_lines = f.readlines()
        new_pos = f.tell()
    if new_lines:
        server_pos = new_pos
        for l in new_lines:
            low = l.lower()
            if any(k in low for k in ['captcha', 'ahk', '캡챠', '클릭', 'blocked', 'stall', 'cooldown']):
                print("[SERVER]", l.rstrip())

    # 2. AHK debug log
    if os.path.exists(ahk_log):
        cur_size = os.path.getsize(ahk_log)
        if cur_size > ahk_pos:
            with open(ahk_log, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(ahk_pos)
                ahk_lines = f.readlines()
            ahk_pos = cur_size
            for l in ahk_lines:
                if l.strip():
                    print("[AHK]   ", l.rstrip())

    # 3. Check captcha windows every 10 seconds
    if check_count % 5 == 0:
        captcha_wins = []
        def callback(hwnd, extra):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if 'Chrome' in title and ('잠시' in title or '기다리' in title):
                    captcha_wins.append((hwnd, title))
        win32gui.EnumWindows(callback, None)
        if captcha_wins:
            for h, t in captcha_wins:
                print(f"[WINDOW] CAPTCHA detected! hwnd={h}")

    # 4. Check success log
    if os.path.exists(success_log):
        with open(success_log, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if lines:
            last = lines[-1].strip()
            if last:
                print(f"[SUCCESS] Latest: {last}")

    time.sleep(2)

print()
print("=" * 60)
print("  Monitor ended after 5 minutes")
print("=" * 60)
