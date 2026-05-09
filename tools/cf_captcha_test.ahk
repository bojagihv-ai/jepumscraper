#Requires AutoHotkey v2.0
#SingleInstance Off

; ─────────────────────────────────────────────────────────────────────────
; cf_captcha_test.ahk  - 캡챠 클릭 기능 진단 테스트 스크립트
;
; 사용법:
;   1) Chrome에서 Gmarket/Auction 캡챠 페이지를 띄워둔다
;   2) 이 스크립트를 더블클릭으로 실행
;   3) 결과 메시지 박스가 뜨면서 진단 결과 표시
; ─────────────────────────────────────────────────────────────────────────

CoordMode "Pixel", "Screen"
CoordMode "Mouse", "Screen"

CF_ORANGE  := 0xF6821F
CF_ORANGE2 := 0xF48120
BOX_GRAY   := 0x767676
BOX_GRAY2  := 0x6B7280
BOX_GRAY3  := 0x9CA3AF
TOL        := 25

report := "=== Cloudflare 캡챠 탐지 진단 ===`n`n"
report .= "화면 해상도: " A_ScreenWidth " x " A_ScreenHeight "`n`n"

; ── CF 주황 로고 탐색 ─────────────────────────────────────
cfX := 0
cfY := 0
try {
    PixelSearch &cfX, &cfY, 0, 0, A_ScreenWidth, A_ScreenHeight, CF_ORANGE, TOL, "Fast"
}
if cfX = 0 {
    try {
        PixelSearch &cfX, &cfY, 0, 0, A_ScreenWidth, A_ScreenHeight, CF_ORANGE2, TOL, "Fast"
    }
}

if cfX > 0 {
    ; 실제 색상 읽기
    actualColor := PixelGetColor(cfX, cfY)
    report .= "✅ CF 주황 로고 발견!`n"
    report .= "   위치: X=" cfX " Y=" cfY "`n"
    report .= "   실제 색상: 0x" Format("{:06X}", actualColor) "`n`n"

    ; ── 체크박스 탐색 ──────────────────────────────────────
    sLeft   := Max(0, cfX - 380)
    sRight  := cfX - 20
    sTop    := Max(0, cfY - 30)
    sBottom := cfY + 30

    bX := 0
    bY := 0
    foundWith := ""
    try {
        PixelSearch &bX, &bY, sLeft, sTop, sRight, sBottom, BOX_GRAY, 20, "Fast"
        if bX > 0
            foundWith := "0x767676 (회색 1)"
    }
    if bX = 0 {
        try {
            PixelSearch &bX, &bY, sLeft, sTop, sRight, sBottom, BOX_GRAY2, 20, "Fast"
            if bX > 0
                foundWith := "0x6B7280 (회색 2)"
        }
    }
    if bX = 0 {
        try {
            PixelSearch &bX, &bY, sLeft, sTop, sRight, sBottom, BOX_GRAY3, 30, "Fast"
            if bX > 0
                foundWith := "0x9CA3AF (연회색)"
        }
    }

    if bX > 0 {
        actualBox := PixelGetColor(bX, bY)
        report .= "✅ 체크박스 네모 발견!`n"
        report .= "   위치: X=" bX " Y=" bY "`n"
        report .= "   탐지 색상: " foundWith "`n"
        report .= "   실제 색상: 0x" Format("{:06X}", actualBox) "`n"
        report .= "   클릭 예정 위치: X=" (bX+10) " Y=" (bY+10) "`n`n"
        report .= "→ 클릭하시겠습니까? (MsgBox에서 예 선택)"

        result := MsgBox(report, "CF 탐지 진단 결과", "YN")
        if result = "Yes" {
            MouseMove bX + 10 - 12, bY + 10 - 4, 6
            Sleep 80
            MouseMove bX + 10 + 2, bY + 10 + 1, 4
            Sleep 60
            Click bX + 10, bY + 10
            MsgBox "클릭 완료! 체크박스가 체크되었는지 확인하세요.", "결과"
        }
    } else {
        ; 탐지 실패 시 실제 색상 샘플링
        report .= "❌ 체크박스 네모 미발견`n"
        report .= "   탐색 범위: X=" sLeft "~" sRight " Y=" sTop "~" sBottom "`n`n"

        ; 탐색 범위 내 실제 색상 샘플 수집
        report .= "── 탐색 범위 내 색상 샘플 ──`n"
        sampleX := sLeft
        Loop 5 {
            sampleX := sLeft + (A_Index * (sRight - sLeft) // 6)
            try {
                c := PixelGetColor(sampleX, cfY)
                report .= "   X=" sampleX " Y=" cfY " → 0x" Format("{:06X}", c) "`n"
            }
        }
        report .= "`n폴백 클릭 위치(로고 -155px): X=" (cfX-155) " Y=" (cfY+5)
        MsgBox report, "CF 탐지 진단 결과", "OK"
    }
} else {
    report .= "❌ CF 주황 로고(#F6821F) 미발견`n`n"
    report .= "화면에 Gmarket/Auction 캡챠 페이지가 열려있는지 확인하세요.`n"
    report .= "또는 브라우저 창이 최소화되어 있지 않은지 확인하세요."
    MsgBox report, "CF 탐지 진단 결과", "OK"
}
