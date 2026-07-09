param(
  [string]$Repo = "",
  [switch]$Visible,
  [switch]$NoDesktopVoice,
  [int]$VoiceInputDevice = 3,
  [int]$VoiceOutputDevice = 5
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Voice = Join-Path $Root "voice"
$Logs = Join-Path $Root "logs"
$Run = Join-Path $Root ".run"
$Window = if ($Visible) { "Normal" } else { "Hidden" }
$Tgpt = Join-Path $HOME ".local\bin\tgpt.exe"
$ChatGptCli = Get-Command chatgpt-cli -ErrorAction SilentlyContinue
$UseChatGptCli = [bool]$env:CHATGPT_SESSION_TOKEN -and [bool]$ChatGptCli
$TextBrainCmd = if ($UseChatGptCli) { "chatgpt-cli" } else { "$Tgpt -q -w" }
$DefaultRepoLine = if ($Repo) { "set `"AI_CTO_DEFAULT_REPO=$Repo`"" } else { "rem AI_CTO_DEFAULT_REPO not set" }
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
New-Item -ItemType Directory -Force -Path $Run | Out-Null

# Load .env (GROQ_API_KEY / OPENROUTER_API_KEY / FIRECRAWL_API_KEY, etc.) into this
# session's environment, without overriding anything already set and without ever
# echoing values. .env is gitignored; never written back to disk here.
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
  foreach ($line in Get-Content $EnvFile) {
    $line = $line.Trim()
    if (-not $line -or $line.StartsWith("#") -or $line -notmatch "=") { continue }
    $k, $v = $line -split "=", 2
    $k = $k.Trim()
    if (-not (Test-Path "env:$k")) { Set-Item -Path "env:$k" -Value $v.Trim() }
  }
}

# Voice brain passthrough lines for the generated .cmd launchers (mirrors the
# CHATGPT_SESSION_TOKEN pattern: reference %VAR%, never write the literal secret).
$VoiceBrainEnvLines = @(
  'if defined GROQ_API_KEY set "GROQ_API_KEY=%GROQ_API_KEY%"',
  'if defined OPENROUTER_API_KEY set "OPENROUTER_API_KEY=%OPENROUTER_API_KEY%"',
  'if defined FIRECRAWL_API_KEY set "FIRECRAWL_API_KEY=%FIRECRAWL_API_KEY%"',
  'if defined AI_CTO_VOICE_BRAIN set "AI_CTO_VOICE_BRAIN=%AI_CTO_VOICE_BRAIN%"',
  'if defined AI_CTO_MEMORY_BRAIN set "AI_CTO_MEMORY_BRAIN=%AI_CTO_MEMORY_BRAIN%"'
) -join "`n"

Write-Host "AI-CTO starting from $Root"
if ($UseChatGptCli) {
  Write-Host "text brain: chatgpt-cli via CHATGPT_SESSION_TOKEN"
} else {
  Write-Host "text brain: tgpt fallback (set CHATGPT_SESSION_TOKEN before start to use ChatGPT CLI)"
}
if ($env:GROQ_API_KEY) {
  $fallbackNote = if ($env:OPENROUTER_API_KEY) { "OpenRouter fallback on" } else { "no fallback key set" }
  Write-Host "voice brain: Groq primary ($fallbackNote)"
} else {
  Write-Host "voice brain: GROQ_API_KEY not set - voice will fail to start (see .env / README)"
}

$VoiceCmd = Join-Path $Run "voice.cmd"
@"
@echo off
cd /d "$Voice"
if defined CHATGPT_SESSION_TOKEN set "TOKEN=%CHATGPT_SESSION_TOKEN%"
set "AI_CTO_TEXT_BRAIN_CMD=$TextBrainCmd"
$VoiceBrainEnvLines
$DefaultRepoLine
".\.venv\Scripts\python.exe" bot.py > "$Logs\voice.log" 2>&1
"@ | Set-Content -Encoding ASCII $VoiceCmd
Start-Process $VoiceCmd -WindowStyle $Window
Write-Host "voice: http://localhost:7860"

if (-not $NoDesktopVoice) {
  $DesktopVoiceCmd = Join-Path $Run "desktop-voice.cmd"
  $DesktopVoiceArgs = ""
  if ($VoiceInputDevice -ge 0) { $DesktopVoiceArgs += " --input-device $VoiceInputDevice" }
  if ($VoiceOutputDevice -ge 0) { $DesktopVoiceArgs += " --output-device $VoiceOutputDevice" }
@"
@echo off
cd /d "$Voice"
$VoiceBrainEnvLines
$DefaultRepoLine
".\.venv\Scripts\python.exe" desktop_voice.py$DesktopVoiceArgs > "$Logs\desktop-voice.log" 2>&1
"@ | Set-Content -Encoding ASCII $DesktopVoiceCmd
  Start-Process $DesktopVoiceCmd -WindowStyle $Window
  Write-Host "desktop voice: started$DesktopVoiceArgs"
}

$WatcherCmd = Join-Path $Run "watcher.cmd"
@"
@echo off
cd /d "$Root"
python scripts\escalation.py watch > "$Logs\watcher.log" 2>&1
"@ | Set-Content -Encoding ASCII $WatcherCmd
Start-Process $WatcherCmd -WindowStyle $Window
Write-Host "blocker watcher: started"

if ($Repo) {
  $WslRepo = (wsl wslpath -a "$Repo").Trim()
  $AoCmd = Join-Path $Run "ao.cmd"
@"
@echo off
wsl -e bash -lc "cd '$WslRepo' && ao start '$WslRepo'" > "$Logs\ao.log" 2>&1
"@ | Set-Content -Encoding ASCII $AoCmd
  Start-Process $AoCmd -WindowStyle $Window
  Write-Host "AO: starting for $Repo"
}

Write-Host "Done. Open http://localhost:7860"
