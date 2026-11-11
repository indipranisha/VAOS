param(
  [Parameter(Mandatory = $true)][int]$LauncherPid,
  [Parameter(Mandatory = $true)][double]$LauncherStartedAt,
  [Parameter(Mandatory = $true)][string]$DataDir,
  [Parameter(Mandatory = $true)][string]$ToolsDir
)

$ErrorActionPreference = 'Stop'
$statePath = Join-Path $DataDir 'admin_tunnel_state.json'
$urlPath = Join-Path $DataDir 'public_admin_url.txt'
$commandPath = Join-Path $DataDir 'admin_tunnel_command.json'
$logsDir = Join-Path $DataDir 'logs'
$stdoutPath = Join-Path $logsDir 'admin_tunnel_stdout.log'
$stderrPath = Join-Path $logsDir 'admin_tunnel_stderr.log'
$script:tunnel = $null
$script:cloudflaredPath = $null
$script:publicUrl = $null

New-Item -ItemType Directory -Path $DataDir,$logsDir -Force | Out-Null

function Write-State([string]$status, [string]$message) {
  $tunnelPid = 0
  if ($script:tunnel -and -not $script:tunnel.HasExited) { $tunnelPid = $script:tunnel.Id }
  $state = @{
    status = $status
    message = $message
    manager_pid = $PID
    launcher_pid = $LauncherPid
    tunnel_pid = $tunnelPid
    heartbeat_at = (Get-Date).ToUniversalTime().ToString('o')
    updated_at = (Get-Date).ToUniversalTime().ToString('o')
  } | ConvertTo-Json -Compress
  $temporary = "$statePath.new"
  Set-Content -LiteralPath $temporary -Value $state -NoNewline -Encoding ascii
  Move-Item -LiteralPath $temporary -Destination $statePath -Force
}

function Touch-Heartbeat {
  $status = 'stopped'
  $message = 'Dashboard tunnel is stopped.'
  try {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $status = [string]$state.status
    $message = [string]$state.message
  } catch { }
  Write-State $status $message
}

function Get-CloudflaredPath {
  foreach ($name in @('cloudflared.exe','cloudflared')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
  }
  $local = Join-Path $ToolsDir 'cloudflared.exe'
  if (Test-Path -LiteralPath $local) { return $local }
  throw 'cloudflared.exe was not found. Start VAOS from its Windows launcher once to prepare the tunnel tool.'
}

function Stop-AdminTunnel {
  try {
    if ($script:tunnel -and -not $script:tunnel.HasExited) {
      Stop-Process -Id $script:tunnel.Id -Force -ErrorAction SilentlyContinue
      for ($attempt = 0; $attempt -lt 30 -and -not $script:tunnel.HasExited; $attempt++) { Start-Sleep -Milliseconds 100 }
    }
  } catch { }
  $script:tunnel = $null
  $script:publicUrl = $null
  Remove-Item -LiteralPath $urlPath -Force -ErrorAction SilentlyContinue
}

function Start-AdminTunnel {
  if ($script:tunnel -and -not $script:tunnel.HasExited) {
    Write-State 'running' 'The public dashboard tunnel is connected.'
    return
  }
  Write-State 'starting' 'Starting a temporary HTTPS tunnel to the VAOS dashboard on port 8000.'
  $script:cloudflaredPath = Get-CloudflaredPath
  Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
  $script:tunnel = Start-Process -FilePath $script:cloudflaredPath -ArgumentList @('tunnel','--no-autoupdate','--url','http://127.0.0.1:8000') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
  $url = $null
  for ($attempt = 0; $attempt -lt 90 -and -not $url; $attempt++) {
    Start-Sleep -Seconds 1
    if ($script:tunnel.HasExited) { break }
    foreach ($logPath in @($stdoutPath,$stderrPath)) {
      if (Test-Path -LiteralPath $logPath) {
        $logText = Get-Content -LiteralPath $logPath -Raw -ErrorAction SilentlyContinue
        if ($logText) {
          $match = [regex]::Match($logText, 'https://[a-z0-9-]+\.trycloudflare\.com')
          if ($match.Success) { $url = $match.Value; break }
        }
      }
    }
  }
  if (-not $url) {
    Write-State 'stopped' 'cloudflared did not provide a public dashboard URL. Check the dashboard tunnel log.'
    throw "cloudflared did not provide a dashboard URL. Check $stderrPath and the network connection."
  }
  $script:publicUrl = $url
  Set-Content -LiteralPath $urlPath -Value $url -NoNewline -Encoding ascii
  Write-State 'running' 'The public dashboard tunnel is connected.'
}

function Invoke-QueuedCommand {
  if (-not (Test-Path -LiteralPath $commandPath)) { return }
  try { $command = Get-Content -LiteralPath $commandPath -Raw | ConvertFrom-Json } catch { $command = $null }
  Remove-Item -LiteralPath $commandPath -Force -ErrorAction SilentlyContinue
  if (-not $command) { return }
  try {
    switch ([string]$command.command) {
      'start' { Start-AdminTunnel }
      'stop' {
        Write-State 'stopping' 'Stopping the public dashboard tunnel.'
        Stop-AdminTunnel
        Write-State 'stopped' 'The public dashboard tunnel is stopped. The camera tunnel is unchanged.'
      }
      'restart' {
        Write-State 'restarting' 'Restarting the public dashboard tunnel. Its URL will change.'
        Stop-AdminTunnel
        Start-AdminTunnel
      }
      default { Write-State 'stopped' 'Unknown dashboard tunnel request.' }
    }
  } catch {
    Stop-AdminTunnel
    Write-State 'stopped' ("Dashboard tunnel failed: {0}" -f $_.Exception.Message)
  }
}

try {
  $launcher = Get-Process -Id $LauncherPid -ErrorAction Stop
  if ($launcher.ProcessName -notmatch '^(powershell|pwsh)$') { throw 'The VAOS launcher process is no longer available.' }
  $launcherStart = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]($LauncherStartedAt * 1000)).UtcDateTime
  if ([Math]::Abs(($launcher.StartTime.ToUniversalTime() - $launcherStart).TotalSeconds) -gt 3) { throw 'The VAOS launcher process identifier has changed.' }
  Write-State 'stopped' 'Dashboard tunnel manager is ready.'
  while ($true) {
    $launcher.Refresh()
    if ($launcher.HasExited) { break }
    Invoke-QueuedCommand
    if ($script:tunnel -and $script:tunnel.HasExited) {
      $script:tunnel = $null
      $script:publicUrl = $null
      Remove-Item -LiteralPath $urlPath -Force -ErrorAction SilentlyContinue
      Write-State 'stopped' 'The dashboard tunnel exited. Use Settings to start it again.'
    }
    Touch-Heartbeat
    Start-Sleep -Seconds 2
  }
} catch {
  try { Write-State 'stopped' ("Dashboard tunnel manager stopped: {0}" -f $_.Exception.Message) } catch { }
} finally {
  Stop-AdminTunnel
  Remove-Item -LiteralPath $commandPath -Force -ErrorAction SilentlyContinue
  Write-State 'stopped' 'The VAOS launcher ended, so the public dashboard tunnel was stopped.'
}
