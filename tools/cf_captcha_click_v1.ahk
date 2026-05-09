#NoEnv
#SingleInstance Off
SetBatchLines, -1
SetTitleMatchMode, 2
CoordMode, Pixel, Screen
CoordMode, Mouse, Screen

; ─────────────────────────────────────────────────────────────────────────
; cf_captcha_click_v1.ahk  (AHK v1)
; Cloudflare Turnstile 체크박스 자동 클릭기
;
; 탐지 전략:
;   1) CF 주황 로고(#F6821F)로 위젯 Y 좌표 확정
;   2) 해당 Y 라인 왼쪽으로 스캔 → 회색 체크박스 테두리(#767676) 탐지
;   3) 발견된 체크박스 좌표 클릭
;   4) 실패 시 고정 오프셋 폴백
;
; 인자: %1%=hwnd  %2%=maxWaitSec  %3%=intervalMs
; 종료코드: 0=성공, 1=타임아웃
; ─────────────────────────────────────────────────────────────────────────

hwnd = %1%
maxWaitSec = %2%
intervalMs = %3%
hwnd := Trim(hwnd)

if (maxWaitSec = "")
    maxWaitSec := 15
else if maxWaitSec is not integer
    ExitApp, 2
if (intervalMs = "")
    intervalMs := 400
else if intervalMs is not integer
    ExitApp, 2

if (hwnd != "") {
    if hwnd is not integer
        ExitApp, 2
    target := "ahk_id " hwnd
    if !WinExist(target)
        ExitApp, 2
    WinActivate, %target%
    WinWaitActive, %target%,, 3
    if ErrorLevel
        ExitApp, 2
}

deadline := A_TickCount + (maxWaitSec * 1000)

; Cloudflare 색상 상수
CF_ORANGE  := 0xF6821F
CF_ORANGE2 := 0xF48120
BOX_GRAY   := 0x767676
BOX_GRAY2  := 0x6B7280
BOX_GRAY3  := 0x9CA3AF

Loop {
    if (A_TickCount > deadline) {
        ExitApp, 1
    }

    ; ── 1) CF 주황 로고 탐색 ──────────────────────────────────
    cfX := 0
    cfY := 0
    PixelSearch, cfX, cfY, 0, 0, %A_ScreenWidth%, %A_ScreenHeight%, %CF_ORANGE%, 25, Fast
    if (cfX = 0)
        PixelSearch, cfX, cfY, 0, 0, %A_ScreenWidth%, %A_ScreenHeight%, %CF_ORANGE2%, 25, Fast

    if (cfX > 0) {
        ; ── 2) CF 위젯 흰 배경 확인 (오탐 방지) ──────────────
        wX := 0
        wY := 0
        wLeft   := cfX - 400
        wTop    := cfY - 40
        wRight  := cfX + 20
        wBottom := cfY + 40
        if (wLeft < 0)
            wLeft := 0
        if (wTop < 0)
            wTop := 0
        PixelSearch, wX, wY, %wLeft%, %wTop%, %wRight%, %wBottom%, 0xFFFFFF, 15, Fast

        if (wX > 0) {
            ; ── 3) 체크박스 회색 네모 탐색 (CF 로고 왼쪽 방향) ──
            sLeft   := cfX - 380
            sTop    := cfY - 30
            sRight  := cfX - 20
            sBottom := cfY + 30
            if (sLeft < 0)
                sLeft := 0
            if (sTop < 0)
                sTop := 0

            bX := 0
            bY := 0

            ; 회색 1
            PixelSearch, bX, bY, %sLeft%, %sTop%, %sRight%, %sBottom%, %BOX_GRAY%, 20, Fast
            if (bX = 0)
                PixelSearch, bX, bY, %sLeft%, %sTop%, %sRight%, %sBottom%, %BOX_GRAY2%, 20, Fast
            if (bX = 0)
                PixelSearch, bX, bY, %sLeft%, %sTop%, %sRight%, %sBottom%, %BOX_GRAY3%, 30, Fast

            if (bX > 0) {
                ; 체크박스 중앙 클릭 (자연스러운 이동)
                clickX := bX + 10
                clickY := bY + 10
                MouseMove, % clickX - 12, % clickY - 4, 6
                Sleep, 80
                MouseMove, % clickX + 2, % clickY + 1, 4
                Sleep, 60
                Click, %clickX%, %clickY%
                Sleep, 200
            } else {
                ; 폴백: CF 로고 기준 고정 오프셋 (-155px)
                clickX := cfX - 155
                clickY := cfY + 5
                MouseMove, % clickX - 10, % clickY - 3, 6
                Sleep, 80
                Click, %clickX%, %clickY%
                Sleep, 200
            }
            ExitApp, 0
        }
    }

    Sleep, %intervalMs%
}
