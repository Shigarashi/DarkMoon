# Toggle "start Darkmoon in the tray when I sign in" for a source (venv) install.
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1          # enable
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Remove  # disable
param([switch]$Remove)
$dir = Split-Path -Parent $PSScriptRoot
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Darkmoon.lnk'
if ($Remove) { Remove-Item $lnk -ErrorAction SilentlyContinue; Write-Host "Autostart removed."; exit }
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = Join-Path $dir '.venv\Scripts\pythonw.exe'
$s.Arguments = '-m app.main --hidden'
$s.WorkingDirectory = $dir
$s.IconLocation = Join-Path $dir 'assets\icon.ico'
$s.Save()
Write-Host "Darkmoon will start in the tray at sign-in."
