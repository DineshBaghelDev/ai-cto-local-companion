$patterns = @("bot.py", "desktop_voice.py", "scripts\\escalation.py watch", "ao start", "\\.run\\voice.cmd", "\\.run\\desktop-voice.cmd", "\\.run\\watcher.cmd", "\\.run\\ao.cmd")

Get-CimInstance Win32_Process |
  Where-Object {
    $cmd = $_.CommandLine
    $cmd -and $_.Name -match "^(cmd|python|pythonw)\.exe$" -and ($patterns | Where-Object { $cmd -match $_ })
  } |
  ForEach-Object {
    Write-Host "Stopping $($_.ProcessId): $($_.CommandLine)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }

Write-Host "Stopped AI-CTO background processes."
