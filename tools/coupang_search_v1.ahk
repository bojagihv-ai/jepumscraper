#NoEnv
#SingleInstance Off
SetBatchLines, -1
SetTitleMatchMode, 2
SendMode, Input

query := %1%
if (query = "")
    ExitApp, 2

WinActivate, ahk_exe chrome.exe
WinWaitActive, ahk_exe chrome.exe,, 3
if ErrorLevel
    ExitApp, 3

oldClipboard := ClipboardAll
Clipboard := query
ClipWait, 2
if ErrorLevel
    ExitApp, 4

SendInput, ^a
Sleep, 120
SendInput, ^v
Sleep, 180
SendInput, {Enter}
Sleep, 300

Clipboard := oldClipboard
ExitApp, 0
