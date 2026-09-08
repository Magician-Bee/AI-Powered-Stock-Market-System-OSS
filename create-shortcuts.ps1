$ProjectRoot = $PSScriptRoot
$Launcher = Join-Path $ProjectRoot "open-stock-ai.ps1"
$PowerShell = (Get-Command powershell.exe).Source
$Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $Launcher + '"'
$Shell = New-Object -ComObject WScript.Shell
$ChineseName = ([char]0x958B).ToString() + ([char]0x555F) + ([char]0x80A1) + ([char]0x5E02) + "AI" + ([char]0x7CFB) + ([char]0x7D71) + ".lnk"

$Shortcuts = @(
    (Join-Path $ProjectRoot "Open Stock AI System.lnk"),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) $ChineseName),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) "Open Stock AI System.lnk")
)

foreach ($ShortcutPath in $Shortcuts) {
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $PowerShell
    $Shortcut.Arguments = $Arguments
    $Shortcut.WorkingDirectory = $ProjectRoot
    $Shortcut.WindowStyle = 7
    $Shortcut.IconLocation = "shell32.dll,220"
    $Shortcut.Description = "Open Stock AI System"
    $Shortcut.Save()
}

Write-Host "Stock AI System shortcuts updated." -ForegroundColor Green
