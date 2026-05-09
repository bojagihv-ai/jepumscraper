#Requires AutoHotkey v2.0
#SingleInstance Off

; ═══════════════════════════════════════════════════════════════
; Cloudflare Turnstile CAPTCHA Auto-Clicker v3
; 검증된 성공 데이터 기반 (2026-05-06 실측)
;
; 성공 기록:
;   CF logo at (1081,833) → checkbox at (841,846) → RESOLVED!
;   화면: 1920x1080, 클라이언트: 1920x1032
;
; 좌표 관계 (실측 검증):
;   CF 로고 → 체크박스: 좌측 240px, 아래 13px
;   위젯 좌상단 → 체크박스: 우측 30px, 아래 27px
;   화면 중앙 → 체크박스 X: 중앙 - 119px
;   체크박스 Y: 클라이언트 높이의 82%
; ═══════════════════════════════════════════════════════════════

hwnd := A_Args.Length >= 1 ? Trim(A_Args[1]) : ""
try {
    maxWaitSec := A_Args.Length >= 2 ? Integer(A_Args[2]) : 15
    intervalMs := A_Args.Length >= 3 ? Integer(A_Args[3]) : 300
} catch {
    ExitApp 2
}

LOG_FILE := "C:\JepumScraper\logs\ahk_debug.log"

Log(msg) {
    global LOG_FILE
    try {
        FileAppend FormatTime(, "HH:mm:ss") " " msg "`n", LOG_FILE
    }
}

; ── 창 활성화 & 최대화 ────────────────────────────
if hwnd {
    if !RegExMatch(hwnd, "^\d+$") {
        ExitApp 2
    }
    target := "ahk_id " hwnd
    if !WinExist(target) {
        ExitApp 2
    }
    try {
        WinActivate target
        if !WinWaitActive(target, , 3) {
            ExitApp 2
        }
        WinMaximize target
        Sleep 600  ; 최대화 + 렌더링 완료 대기
    } catch {
        ExitApp 2
    }
}

CoordMode "Pixel", "Screen"
CoordMode "Mouse", "Screen"

deadline := A_TickCount + (maxWaitSec * 1000)

; ── 색상 상수 (실측 검증) ─────────────────────────
CF_ORANGE    := 0xF6821F   ; RGB(246,130,31) - 정확한 CF 로고색
CF_ORANGE2   := 0xF6913A   ; RGB(246,145,58) - 밝은 변형
CF_ORANGE3   := 0xE87722   ; RGB(232,119,34) - DPI 변형
ORANGE_TOL   := 22

WIDGET_GRAY  := 0xDADADA   ; RGB(218,218,218) - 위젯 테두리
WIDGET_TOL   := 18

; ── 자연스러운 클릭 ───────────────────────────────
ClickAt(cx, cy) {
    MouseMove cx - 20, cy - 6, 4
    Sleep 50
    MouseMove cx - 3, cy - 1, 3
    Sleep 30
    MouseMove cx + 1, cy, 2
    Sleep 20
    Click cx, cy
    Sleep 200
}

; ── 클라이언트 영역 취득 ──────────────────────────
cX := 0, cY := 0, cW := A_ScreenWidth, cH := A_ScreenHeight
if hwnd {
    try {
        WinGetClientPos &cX, &cY, &cW, &cH, "ahk_id " hwnd
    }
}

Log("=== v3 START === hwnd=" hwnd " client=" cX "," cY " " cW "x" cH)

; ═══════════════════════════════════════════════════════════════
; 메인 루프 - 4중 탐지 전략, 어떤 것이든 하나만 성공하면 클릭
; ═══════════════════════════════════════════════════════════════
Loop {
    if A_TickCount > deadline {
        Log("TIMEOUT after " maxWaitSec "s")
        ExitApp 1
    }

    ; 탐색 범위: 하단 55%만 (상단의 G마켓/옥션 자체 로고 완전 회피)
    scanLeft   := Max(cX, cX + (cW / 2) - 250)
    scanRight  := Min(cX + cW, cX + (cW / 2) + 250)
    scanTop    := cY + (cH * 0.45)
    scanBottom := cY + cH

    ; ──────────────────────────────────────────────
    ; 전략 A: CF 주황색 로고 탐색 (가장 정확)
    ; 성공 실적: 1081,833 → 841,846 (2/2 성공)
    ; 관계: 체크박스 = 로고 좌측 240px, 아래 13px
    ; ──────────────────────────────────────────────
    orangeList := [CF_ORANGE, CF_ORANGE2, CF_ORANGE3]
    for idx, oc in orangeList {
        if PixelSearch(&fX, &fY, scanLeft, scanTop, scanRight, scanBottom, oc, ORANGE_TOL) {
            ; 진짜 CF 로고인지 검증: 주변 10px 내에 추가 주황색 픽셀
            if PixelSearch(&t1, &t2, fX - 10, fY - 10, fX + 10, fY + 10, CF_ORANGE, ORANGE_TOL + 8) {
                cbX := fX - 240
                cbY := fY + 13
                Log("HIT-A: CF#" idx " at " fX "," fY " -> cb=" cbX "," cbY)
                ClickAt(cbX, cbY)
                ExitApp 0
            }
        }
    }

    ; ──────────────────────────────────────────────
    ; 전략 B: 위젯 상단 수평 테두리 탐색
    ; 위젯 상단 = ~300px 길이의 밝은 회색선
    ; 체크박스 = 테두리 좌측 + 30px, 아래 + 27px
    ; ──────────────────────────────────────────────
    if PixelSearch(&bX, &bY, scanLeft, scanTop, scanRight, scanBottom, WIDGET_GRAY, WIDGET_TOL) {
        ; 같은 Y에서 우측 250px 지점에도 같은 색이 있으면 수평 테두리
        if PixelSearch(&t1, &t2, bX + 240, bY - 2, bX + 310, bY + 2, WIDGET_GRAY, WIDGET_TOL) {
            cbX := bX + 30
            cbY := bY + 27
            Log("HIT-B: WidgetTop at " bX "," bY " -> cb=" cbX "," cbY)
            ClickAt(cbX, cbY)
            ExitApp 0
        }
    }

    ; ──────────────────────────────────────────────
    ; 전략 C: 위젯 좌측 수직 테두리 탐색
    ; 세로로 50px+ 이어지는 밝은 회색 = 위젯 좌측벽
    ; 체크박스 = 좌측벽 우측 30px, 아래 27px
    ; ──────────────────────────────────────────────
    vScanL := Max(cX, cX + (cW / 2) - 180)
    vScanR := cX + (cW / 2) - 80
    if PixelSearch(&vX, &vY, vScanL, scanTop, vScanR, scanBottom, WIDGET_GRAY, WIDGET_TOL) {
        if PixelSearch(&t1, &t2, vX - 2, vY + 45, vX + 2, vY + 65, WIDGET_GRAY, WIDGET_TOL) {
            cbX := vX + 30
            cbY := vY + 27
            Log("HIT-C: WidgetLeft at " vX "," vY " -> cb=" cbX "," cbY)
            ClickAt(cbX, cbY)
            ExitApp 0
        }
    }

    ; ──────────────────────────────────────────────
    ; 전략 D: 수학적 확정 폴백 (3초 후 발동)
    ; 실측 검증: 체크박스 X = 화면중앙 - 119, Y = 클라이언트높이 * 0.82
    ; 이것은 성공 기록 (841,846)과 정확히 일치하는 수학 공식
    ;   960 - 119 = 841 ✓
    ;   1032 * 0.82 = 846 ✓
    ; ──────────────────────────────────────────────
    if (A_TickCount > deadline - ((maxWaitSec - 3) * 1000)) {
        cbX := cX + (cW / 2) - 119
        cbY := cY + (cH * 0.82)
        Log("HIT-D: Math at " cbX "," cbY " (center=" cX + (cW/2) " h82%=" cY + (cH*0.82) ")")
        ClickAt(cbX, cbY)
        ExitApp 0
    }

    Sleep intervalMs
}
