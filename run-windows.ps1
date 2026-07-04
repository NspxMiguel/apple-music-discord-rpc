$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $projectDir "music-rpc-windows.py"
$pythonw = "C:\Users\User\AppData\Local\Programs\Python\Python314\pythonw.exe"

if (-not (Test-Path -LiteralPath $pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $pythonw = $cmd.Source
    } else {
        throw "pythonw.exe not found. Install Python 3.11+ and add it to PATH."
    }
}

Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match "^(python|pythonw|wscript)\.exe$" } |
    Where-Object { $_.CommandLine -match "music-rpc-windows\.py|apple-music-rpc|apple-music-discord-rpc" } |
    Where-Object { $_.ProcessId -ne $PID } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force
        } catch {}
    }

Start-Process -FilePath $pythonw -ArgumentList "`"$scriptPath`"" -WorkingDirectory $projectDir -WindowStyle Hidden
