#Requires AutoHotkey v2.0
#SingleInstance Off

query := A_Args.Length >= 1 ? A_Args[1] : ""
if !query {
    ExitApp 2
}

WinActivate "ahk_exe chrome.exe"
if !WinWaitActive("ahk_exe chrome.exe",, 3) {
    ExitApp 3
}

oldClipboard := ClipboardAll()
A_Clipboard := query
if !ClipWait(2) {
    ExitApp 4
}

Send "^a"
Sleep 120
Send "^v"
Sleep 180
Send "{Enter}"
Sleep 300

A_Clipboard := oldClipboard
ExitApp 0
