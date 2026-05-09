#Requires AutoHotkey v2.0
#SingleInstance Off

; ─────────────────────────────────────────────────────────────────────────
; naver_login_click_v2.ahk
; 전략:
;   1) 네이버 로그인 Chrome 창 찾아서 활성화
;   2) 창 위치/크기 기반으로 "로그인" 버튼 좌표 계산
;   3) 해당 위치 주변에서 녹색(#03C75A) 탐색 → 발견시 클릭
;   4) 색상 탐지 실패해도 계산된 위치 직접 클릭
;   5) 최후 폴백: Enter 키 전송
; ─────────────────────────────────────────────────────────────────────────

maxWaitSec := (A_Args.Length >= 2 && A_Args[2] != "") ? Integer(A_Args[2]) : 8

CoordMode "Pixel", "Screen"
CoordMode "Mouse", "Screen"
SetTitleMatchMode 2

; ── 1) 네이버 로그인 창 찾기 ──────────────────────────────────────────
naverWin := 0
for hwnd in WinGetList() {
    t := WinGetTitle("ahk_id " hwnd)
    if (InStr(t, "네이버") && InStr(t, "로그인")) || InStr(t, "nid.naver") {
        naverWin := hwnd
        break
    }
}
if naverWin = 0 {
    ExitApp 1
}

WinActivate "ahk_id " naverWin
WinWaitActive "ahk_id " naverWin,, 3
Sleep 600

; ── 2) 창 크기/위치로 버튼 좌표 계산 ────────────────────────────────
WinGetPos &wX, &wY, &wW, &wH, "ahk_id " naverWin

; Chrome 상단 UI (탭+주소창) 높이 약 88px
chromeUI := 88
contentTop := wY + chromeUI
contentH   := wH - chromeUI

; 로그인 버튼: 페이지 세로 기준 약 55%, 가로 중앙
btnCenterX := wX + (wW // 2)
btnCenterY := contentTop + Integer(contentH * 0.555)

; ── 3) 버튼 주변에서 녹색 탐색 ───────────────────────────────────────
NAVER_GREEN  := 0x03C75A
NAVER_GREEN2 := 0x04CE5E
TOL := 35

; 탐색 범위: 버튼 예상 위치 ±150px
sL := btnCenterX - 200
sR := btnCenterX + 200
sT := btnCenterY - 40
sB := btnCenterY + 40

fx := 0
fy := 0
found := PixelSearch(&fx, &fy, sL, sT, sR, sB, NAVER_GREEN, TOL)
if !found
    found := PixelSearch(&fx, &fy, sL, sT, sR, sB, NAVER_GREEN2, TOL)

; ── 4) 클릭 ──────────────────────────────────────────────────────────
if found {
    ; 녹색 픽셀 기준 버튼 중앙으로 오프셋
    clickX := fx + 40
    clickY := fy + 12
} else {
    ; 색상 못 찾아도 계산 좌표 직접 클릭
    clickX := btnCenterX
    clickY := btnCenterY
}

MouseMove clickX - 20, clickY - 8, 7
Sleep 100
MouseMove clickX, clickY, 5
Sleep 80
Click clickX, clickY
Sleep 400

; ── 5) 클릭 후에도 여전히 로그인 창이면 Enter 폴백 ──────────────────
titleAfter := WinGetTitle("ahk_id " naverWin)
if InStr(titleAfter, "로그인") {
    Send "{Enter}"
    Sleep 300
}

ExitApp 0
