# Creates Desktop + Start Menu shortcuts for a source (venv) install.
$dir = Split-Path -Parent $PSScriptRoot
$ws = New-Object -ComObject WScript.Shell
$targets = @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))
foreach ($p in $targets) {
  $s = $ws.CreateShortcut((Join-Path $p 'Darkmoon.lnk'))
  $s.TargetPath = Join-Path $dir '.venv\Scripts\pythonw.exe'
  $s.Arguments = '-m app.main'
  $s.WorkingDirectory = $dir
  $s.IconLocation = Join-Path $dir 'assets\icon.ico'
  $s.Description = 'Darkmoon'
  $s.Save()
}
Write-Host "  Shortcuts created on Desktop and Start Menu."
