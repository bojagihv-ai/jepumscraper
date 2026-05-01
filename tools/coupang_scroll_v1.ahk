#NoEnv
#SingleInstance Off
SetBatchLines, -1
SetTitleMatchMode, 2

hwnd := %1%
steps := %2%
delayMs := %3%
homeFirst := %4%
if (steps = "")
    steps := 12
if (delayMs = "")
    delayMs := 700
if (homeFirst = "")
    homeFirst := 1

WinActivate, ahk_id %hwnd%
WinWaitActive, ahk_id %hwnd%,, 3
if (homeFirst != 0)
{
    SendInput, ^{Home}
    Sleep, 700
}

Loop, %steps%
{
    ControlSend,, {WheelDown 7}, ahk_id %hwnd%
    Sleep, %delayMs%
}
ExitApp, 0
