#Requires AutoHotkey v2.0
#SingleInstance Off

hwnd := A_Args.Length >= 1 ? A_Args[1] : ""
steps := A_Args.Length >= 2 ? Integer(A_Args[2]) : 12
delayMs := A_Args.Length >= 3 ? Integer(A_Args[3]) : 700
homeFirst := A_Args.Length >= 4 ? Integer(A_Args[4]) : 1

if !hwnd {
    ExitApp 2
}

WinActivate "ahk_id " hwnd
WinWaitActive "ahk_id " hwnd,, 3
if homeFirst {
    Send "^{Home}"
    Sleep 700
}

Loop steps {
    ControlSend "{WheelDown 7}",, "ahk_id " hwnd
    Sleep delayMs
}
ExitApp 0
