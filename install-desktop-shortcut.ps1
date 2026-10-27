$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$desktopDirectory = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopDirectory 'Job Application Pipeline.lnk'
$launcherPath = Join-Path $taskRoot 'launch-dashboard.ps1'
$iconPath = Join-Path $taskRoot 'app\static\dashboard.ico'
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (!(Test-Path -LiteralPath $launcherPath)) { throw 'Dashboard launcher is missing.' }

$shortcutShell = New-Object -ComObject WScript.Shell
$shortcut = $shortcutShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $windowsPowerShell
$shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launcherPath + '"'
$shortcut.WorkingDirectory = $taskRoot
$shortcut.Description = 'Start the local dashboard and open it in your browser. Resume saved companies from the dashboard.'
$shortcut.WindowStyle = 7
if (Test-Path -LiteralPath $iconPath) { $shortcut.IconLocation = "$iconPath,0" }
$shortcut.Save()
Write-Output "Desktop shortcut installed: $shortcutPath"
