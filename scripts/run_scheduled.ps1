$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
$log = "$root\logs\scheduled-$(Get-Date -Format 'yyyyMMdd').log"
"=== Run started $(Get-Date -Format s) ===" | Out-File -Append -Encoding utf8 $log
& "C:\Users\hp\.local\bin\uv.exe" run python main.py --image-provider openai --tts-provider openai --tts-voice marin --upload --youtube-privacy public 2>&1 | Out-File -Append -Encoding utf8 $log
"=== Run finished $(Get-Date -Format s) (exit $LASTEXITCODE) ===" | Out-File -Append -Encoding utf8 $log
