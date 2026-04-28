"""
launcher.py - JepumScraper 통합 런처
─────────────────────────────────────
Flask 서버를 백그라운드 스레드로 시작하고,
브라우저로 웹 UI를 열어주는 tkinter 런처 앱.

ProScraper의 크롤링 엔진 + JepumSangse의 상품 소싱 UI를 통합.
"""
import os
import sys
import threading
import time
import webbrowser
import subprocess
import tkinter as tk
from tkinter import ttk, font as tkfont
import logging

# 현재 디렉토리를 JepumScraper 루트로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

HOST = '127.0.0.1'
PORT = 5002  # anti-JepumSangse(5000), product-detail-generator(5001)과 충돌 방지


class JepumScraperLauncher:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('JepumScraper 런처')
        self.root.geometry('480x320')
        self.root.resizable(False, False)
        self.root.configure(bg='#1a1a2e')

        # 아이콘 설정 (있는 경우)
        ico_path = os.path.join(BASE_DIR, 'app_icon.ico')
        if os.path.exists(ico_path):
            try:
                self.root.iconbitmap(ico_path)
            except Exception:
                pass

        self._flask_thread = None
        self._server_ready = False
        self._build_ui()

    def _build_ui(self):
        root = self.root

        # 타이틀
        title_font = tkfont.Font(family='맑은 고딕', size=18, weight='bold')
        sub_font   = tkfont.Font(family='맑은 고딕', size=11)
        btn_font   = tkfont.Font(family='맑은 고딕', size=12, weight='bold')
        log_font   = tkfont.Font(family='Consolas', size=9)

        tk.Label(root, text='🔍 JepumScraper', font=title_font,
                 fg='#e94560', bg='#1a1a2e').pack(pady=(24, 2))
        tk.Label(root, text='ProScraper 엔진 × 점프상세 소싱 통합',
                 font=sub_font, fg='#a0a0c0', bg='#1a1a2e').pack(pady=(0, 16))

        # 상태 레이블
        self._status_var = tk.StringVar(value='대기 중...')
        self._status_lbl = tk.Label(root, textvariable=self._status_var,
                                     font=sub_font, fg='#f0c060', bg='#1a1a2e')
        self._status_lbl.pack(pady=(0, 8))

        # 프로그레스 바
        self._progress = ttk.Progressbar(root, mode='indeterminate', length=360)
        self._progress.pack(pady=(0, 16))

        # 로그 박스
        log_frame = tk.Frame(root, bg='#0f0f1f')
        log_frame.pack(fill='x', padx=24)
        self._log_text = tk.Text(log_frame, height=4, bg='#0f0f1f', fg='#80ff80',
                                  font=log_font, state='disabled', bd=0,
                                  insertbackground='#80ff80')
        self._log_text.pack(fill='x')

        # 버튼 프레임
        btn_frame = tk.Frame(root, bg='#1a1a2e')
        btn_frame.pack(pady=14)

        self._open_btn = tk.Button(
            btn_frame, text='🌐 브라우저 열기',
            font=btn_font, bg='#16213e', fg='#e94560',
            activebackground='#e94560', activeforeground='white',
            relief='flat', padx=18, pady=8,
            state='disabled',
            command=self._open_browser,
        )
        self._open_btn.pack(side='left', padx=8)

        tk.Button(
            btn_frame, text='✕ 종료',
            font=btn_font, bg='#16213e', fg='#606080',
            activebackground='#e94560', activeforeground='white',
            relief='flat', padx=14, pady=8,
            command=self._quit,
        ).pack(side='left', padx=8)

    def _log(self, msg: str):
        def _do():
            self._log_text.configure(state='normal')
            self._log_text.insert('end', msg + '\n')
            self._log_text.see('end')
            self._log_text.configure(state='disabled')
        self.root.after(0, _do)

    def _set_status(self, msg: str, color: str = '#f0c060'):
        def _do():
            self._status_var.set(msg)
            self._status_lbl.configure(fg=color)
        self.root.after(0, _do)

    def _start_flask(self):
        """Flask 서버를 별도 스레드에서 시작."""
        self._set_status('Flask 서버 시작 중...')
        self._log(f'서버 시작: http://{HOST}:{PORT}')

        try:
            from main import app
            import logging as _logging
            _logging.getLogger('werkzeug').setLevel(_logging.ERROR)

            # 서버 준비 완료 신호 (0.5초 후 UI 업데이트)
            def _mark_ready():
                time.sleep(0.5)
                self._server_ready = True
                self._set_status(f'✅ 서버 실행 중  http://{HOST}:{PORT}', '#60ff80')
                self._log('서버 준비 완료! 브라우저 열기 버튼을 누르세요.')
                self.root.after(0, lambda: self._progress.stop())
                self.root.after(0, lambda: self._open_btn.configure(state='normal'))
                # 자동으로 브라우저 열기
                self.root.after(800, self._open_browser)

            threading.Thread(target=_mark_ready, daemon=True).start()

            app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)

        except Exception as e:
            self._set_status(f'❌ 서버 오류: {e}', '#ff6060')
            self._log(f'오류: {e}')
            self.root.after(0, lambda: self._progress.stop())

    def _open_browser(self):
        url = f'http://{HOST}:{PORT}'
        self._log(f'브라우저 열기: {url}')
        webbrowser.open(url)

    def _quit(self):
        self.root.destroy()
        os._exit(0)

    def run(self):
        # Flask 서버를 백그라운드에서 시작
        self._flask_thread = threading.Thread(target=self._start_flask, daemon=True)
        self._flask_thread.start()
        self._progress.start(12)

        # Ctrl+C 인터럽트 방지
        self.root.protocol('WM_DELETE_WINDOW', self._quit)
        self.root.mainloop()


if __name__ == '__main__':
    # 로그 설정
    log_path = os.path.join(BASE_DIR, 'logs', 'launcher.log')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_path, encoding='utf-8'),
            logging.StreamHandler(),
        ]
    )

    launcher = JepumScraperLauncher()
    launcher.run()
