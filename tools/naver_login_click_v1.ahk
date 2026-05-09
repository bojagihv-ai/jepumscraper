#NoEnv
#SingleInstance Off
SetBatchLines, -1
SetTitleMatchMode, 2
CoordMode, Pixel, Screen
CoordMode, Mouse, Screen

; ─────────────────────────────────────────────────────────────────────────
; naver_login_click_v1.ahk (AHK v1)
; 네이버 로그인 창의 "로그인" 버튼 자동 클릭기
; ─────────────────────────────────────────────────────────────────────────

hwnd       := %1%
maxWaitSec := %2%
intervalMs := %3%

if (maxWaitSec = "")
    maxWaitSec := 5
if (intervalMs = "")
    intervalMs := 400

if (hwnd != "") {
    WinActivate, ahk_id %hwnd%
    WinWaitActive, ahk_id %hwnd%,, 3
}

deadline := A_TickCount + (maxWaitSec * 1000)

; 네이버 메인 녹색 (BGR format for AHK v1 PixelSearch unless RGB mode is used.
; Let's use RGB mode for consistency)
NAVER_GREEN := 0x03C75A
NAVER_GREEN2 := 0x04CE5E
TOL := 20

Loop {
    if (A_TickCount > deadline) {
        ExitApp, 1
    }

    fx := 0
    fy := 0
    ; RGB mode required for 0x03C75A
    PixelSearch, fx, fy, 0, 0, %A_ScreenWidth%, %A_ScreenHeight%, %NAVER_GREEN%, %TOL%, Fast RGB
    if (fx = 0)
        PixelSearch, fx, fy, 0, 0, %A_ScreenWidth%, %A_ScreenHeight%, %NAVER_GREEN2%, %TOL%, Fast RGB

    if (fx > 0) {
        clickX := fx + 30
        clickY := fy + 15
        
        MouseMove, % clickX - 10, % clickY - 5, 6
        Sleep, 80
        MouseMove, % clickX + 2, % clickY + 2, 4
        Sleep, 60
        Click, %clickX%, %clickY%
        Sleep, 300
        ExitApp, 0
    }

    Sleep, %intervalMs%
}
