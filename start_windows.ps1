$ErrorActionPreference = 'Stop'
# PowerShell 7.3+ can turn a native program's nonzero exit into a terminating
# error when this preference is enabled. Python import probes intentionally
# use a nonzero exit to detect packages that have not been installed yet.
$PSNativeCommandUseErrorActionPreference = $false
Set-Location -LiteralPath $PSScriptRoot

$children = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$publicUrlPath = Join-Path $PSScriptRoot 'data\public_camera_url.txt'
$tunnelStatePath = Join-Path $PSScriptRoot 'data\tunnel_state.json'
$launcherCommandPath = Join-Path $PSScriptRoot 'data\launcher_command.json'
$runId = '{0}_{1}' -f (Get-Date -Format 'yyyyMMdd_HHmmss'), $PID
$logsDir = Join-Path $PSScriptRoot (Join-Path 'data\logs' $runId)
$adminLog = Join-Path $logsDir 'admin_service.log'
$adminErrorLog = Join-Path $logsDir 'admin_service_error.log'
$cameraLog = Join-Path $logsDir 'camera_service.log'
$cameraErrorLog = Join-Path $logsDir 'camera_service_error.log'
$tunnelOut = Join-Path $logsDir 'cloudflared_stdout.log'
$tunnelErr = Join-Path $logsDir 'cloudflared_stderr.log'
$exitCode = 0
$startupPhase = 'preparing'
$admin = $null
$camera = $null
$tunnel = $null
$python = $null
$cloudflaredPath = $null
$publicUrl = $null

# Avoid a confusing file-lock error when an earlier launch still owns the ports.
$adminHealthy = $false
$cameraHealthy = $false
$normalizedRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\','/').ToLowerInvariant()
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$instanceBytes = [System.Text.Encoding]::UTF8.GetBytes($normalizedRoot)
$instanceId = ([System.BitConverter]::ToString($sha256.ComputeHash($instanceBytes))).Replace('-','').ToLowerInvariant().Substring(0,16)
$sha256.Dispose()
$adminStatus = $null
$cameraStatus = $null
try { $adminStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2; $adminHealthy = $adminStatus.app -in @('vaos-admin','local-camera-recognition-poc') } catch { }
try { $cameraStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:8001/health' -TimeoutSec 2; $cameraHealthy = $cameraStatus.app -eq 'camera-station' } catch { }
$adminThisFolder = $adminHealthy -and $adminStatus.instance_id -eq $instanceId
$cameraThisFolder = $cameraHealthy -and $cameraStatus.instance_id -eq $instanceId
if ($adminThisFolder -and $cameraThisFolder) {
  Write-Host 'This copy of the camera app is already running. Opening its dashboard.' -ForegroundColor Yellow
  Start-Process 'http://127.0.0.1:8000'
  exit 0
}
if ($adminHealthy -or $cameraHealthy) {
  Write-Host 'Found an older or partial camera app instance. Restarting only its verified app services so this folder can start.' -ForegroundColor Yellow
  $servicesToReplace = @()
  if ($adminHealthy) { $servicesToReplace += @{ Port = 8000; Module = 'main:app' } }
  if ($cameraHealthy) { $servicesToReplace += @{ Port = 8001; Module = 'camera_service:app' } }
  foreach ($service in $servicesToReplace) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $service.Port -ErrorAction SilentlyContinue)
    if (-not $listeners.Count) { throw "The app on port $($service.Port) responded to health checks, but its listener could not be identified. Close the older app window and try again." }
    foreach ($listenerPid in ($listeners.OwningProcess | Sort-Object -Unique)) {
      $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerPid" -ErrorAction SilentlyContinue
      if (-not $owner -or $owner.Name -notmatch '^pythonw?\.exe$' -or $owner.CommandLine -notmatch '(?i)-m\s+uvicorn\s+' -or $owner.CommandLine -notlike "*$($service.Module)*") {
        throw "Port $($service.Port) is occupied, but the process is not a verified camera app service. Stop that process manually; no unrelated process was stopped."
      }
      Write-Host "Stopping the older camera app service on port $($service.Port) (process $listenerPid)..."
      Stop-Process -Id $listenerPid -Force
    }
  }
  for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $remaining = @(Get-NetTCPConnection -State Listen -LocalPort 8000,8001 -ErrorAction SilentlyContinue)
    if (-not $remaining.Count) { break }
    Start-Sleep -Milliseconds 500
  }
  $remaining = @(Get-NetTCPConnection -State Listen -LocalPort 8000,8001 -ErrorAction SilentlyContinue)
  if ($remaining.Count) { throw 'The previous camera app still holds port 8000 or 8001. Close its launcher window, then run this launcher again.' }
}
foreach ($port in @(8000,8001)) {
  $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
  if ($listeners.Count) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listeners[0].OwningProcess)" -ErrorAction SilentlyContinue
    $ownerName = if ($owner) { $owner.Name } else { 'unknown process' }
    throw "Port $port is used by $ownerName (process $($listeners[0].OwningProcess)), not by this camera app. Close that program or free port $port, then run the launcher again."
  }
}

function Set-TunnelState([string]$status, [string]$message) {
  $state = @{ status = $status; message = $message; updated_at = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json -Compress
  $temporary = "$tunnelStatePath.new"
  Set-Content -LiteralPath $temporary -Value $state -NoNewline -Encoding ascii
  Move-Item -LiteralPath $temporary -Destination $tunnelStatePath -Force
}

function Stop-AppServices {
  foreach ($service in @($script:admin,$script:camera)) {
    try { if ($service -and -not $service.HasExited) { Stop-Process -Id $service.Id -Force -ErrorAction SilentlyContinue } } catch { }
  }
  foreach ($port in @(8000,8001)) {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
      $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
      if (-not $listeners.Count) { break }
      Start-Sleep -Milliseconds 200
    }
  }
}

function Start-AppServices {
  $script:admin = Start-Process -FilePath $script:python -ArgumentList @('-m','uvicorn','main:app','--host','127.0.0.1','--port','8000') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $adminLog -RedirectStandardError $adminErrorLog -PassThru
  $children.Add($script:admin)
  $null = Wait-ForLocalService 'http://127.0.0.1:8000/health' 'vaos-admin' $script:admin 'VAOS admin service'
  $script:camera = Start-Process -FilePath $script:python -ArgumentList @('-m','uvicorn','camera_service:app','--host','127.0.0.1','--port','8001') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $cameraLog -RedirectStandardError $cameraErrorLog -PassThru
  $children.Add($script:camera)
  $null = Wait-ForLocalService 'http://127.0.0.1:8001/health' 'camera-station' $script:camera 'Public camera service'
}

function Stop-Tunnel {
  try {
    if ($script:tunnel -and -not $script:tunnel.HasExited) {
      Stop-Process -Id $script:tunnel.Id -Force -ErrorAction SilentlyContinue
      for ($attempt = 0; $attempt -lt 30 -and -not $script:tunnel.HasExited; $attempt++) { Start-Sleep -Milliseconds 100 }
    }
  } catch { }
  $script:tunnel = $null
  $script:publicUrl = $null
  Remove-Item -LiteralPath $publicUrlPath -Force -ErrorAction SilentlyContinue
}

function Start-Tunnel {
  if ($script:tunnel -and -not $script:tunnel.HasExited) {
    Set-TunnelState 'running' 'Camera tunnel is connected.'
    return
  }
  Set-TunnelState 'starting' 'Starting the temporary camera tunnel.'
  Write-Host 'Creating a temporary HTTPS tunnel to the camera-only service on port 8001...'
  Remove-Item -LiteralPath $tunnelOut,$tunnelErr -Force -ErrorAction SilentlyContinue
  $script:tunnel = Start-Process -FilePath $script:cloudflaredPath -ArgumentList @('tunnel','--no-autoupdate','--protocol','http2','--url','http://127.0.0.1:8001') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $tunnelOut -RedirectStandardError $tunnelErr -PassThru
  $children.Add($script:tunnel)
  $url = $null
  for ($attempt = 0; $attempt -lt 90 -and -not $url; $attempt++) {
    Start-Sleep -Seconds 1
    if ($script:tunnel.HasExited) { break }
    foreach ($logPath in @($tunnelOut,$tunnelErr)) {
      if (Test-Path -LiteralPath $logPath) {
        $logText = Get-Content -LiteralPath $logPath -Raw -ErrorAction SilentlyContinue
        if (-not [string]::IsNullOrWhiteSpace($logText)) {
          $match = [regex]::Match($logText, 'https://[a-z0-9-]+\.trycloudflare\.com')
          if ($match.Success) { $url = $match.Value; break }
        }
      }
    }
  }
  if (-not $url) {
    Set-TunnelState 'stopped' 'cloudflared did not provide a public HTTPS address. Check the tunnel log and network connection.'
    throw "cloudflared did not provide a public URL. Check the logs in $logsDir and your network connection."
  }
  $script:publicUrl = $url
  Set-Content -LiteralPath $publicUrlPath -Value $url -NoNewline -Encoding ascii
  Set-TunnelState 'running' 'Camera tunnel is connected.'
  Write-Host "Temporary camera URL: $url"
}

function Stop-LocalServices {
  Stop-AppServices
  Stop-Tunnel
  Set-TunnelState 'stopped' 'Camera tunnel is stopped.'
}

function Wait-ForLocalService([string]$url, [string]$expectedApp, [System.Diagnostics.Process]$process, [string]$label) {
  for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($process.HasExited) { throw "$label stopped during startup. Check this run's log folder: $logsDir" }
    try {
      $status = Invoke-RestMethod -Uri $url -TimeoutSec 2
      if ($status.app -eq $expectedApp) { return $status }
    } catch { }
    Start-Sleep -Seconds 1
  }
  throw "$label did not become ready within 30 seconds. Check this run's log folder: $logsDir"
}

try {
  $startupPhase = 'checking Python setup'
  if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3.12 -m venv .venv }
    else { & python -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 was not found. Install it from https://www.python.org/downloads/ and enable Add Python to PATH.' }
  }
  $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
  $startupPhase = 'checking required packages and optional GPU support'
  New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'data') -Force | Out-Null
  $nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
  if (-not $nvidiaSmi) { $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue }
  & $python -c 'import cv2, fastapi, jinja2, multipart, numpy, psutil, qrcode, uvicorn'
  if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing required camera and face-recognition packages (first setup only)...'
    & $python -m pip install --disable-pip-version-check --timeout 30 --retries 3 -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Required app packages could not be installed. Check internet access and try again.' }
    & $python -c 'import cv2, fastapi, jinja2, multipart, numpy, psutil, qrcode, uvicorn'
    if ($LASTEXITCODE -ne 0) { throw 'Required app packages were installed but could not be imported. See pip output above.' }
  }

  # Object detection is optional: install its runtime when possible, but let
  # the face-enrollment app start on CPU if PyTorch/CUDA setup is unavailable.
  $torchReady = $false
  $cudaReady = $false
  $gpuCheck = Join-Path $PSScriptRoot 'check_gpu_runtime.py'
  $runtimeStatus = (& $python $gpuCheck) -join ''
  $torchReady = $LASTEXITCODE -eq 0 -and $runtimeStatus.Trim() -in @('cpu','cuda')
  $cudaReady = $torchReady -and $runtimeStatus.Trim() -eq 'cuda'
  if ($nvidiaSmi -and -not $cudaReady) {
    Write-Host 'Preparing NVIDIA GPU 0 support for YOLO26 object detection...'
    & $python -m pip install --disable-pip-version-check --timeout 30 --retries 2 torch==2.13.0 torchvision==0.28.0 --index-url 'https://download.pytorch.org/whl/cu132'
    if ($LASTEXITCODE -eq 0) {
      $runtimeStatus = (& $python $gpuCheck) -join ''
      $torchReady = $LASTEXITCODE -eq 0 -and $runtimeStatus.Trim() -in @('cpu','cuda')
      $cudaReady = $torchReady -and $runtimeStatus.Trim() -eq 'cuda'
    }
  }
  if (-not $torchReady) {
    Write-Host 'Installing CPU PyTorch for optional YOLO26 object detection...'
    & $python -m pip install --disable-pip-version-check --timeout 30 --retries 2 torch==2.13.0 torchvision==0.28.0 --index-url 'https://download.pytorch.org/whl/cpu'
    if ($LASTEXITCODE -eq 0) {
      $runtimeStatus = (& $python $gpuCheck) -join ''
      $torchReady = $LASTEXITCODE -eq 0 -and $runtimeStatus.Trim() -in @('cpu','cuda')
    }
  }
  if ($torchReady) {
    & $python -c 'import matplotlib, PIL, yaml, requests, scipy, psutil, polars, ultralytics_thop, tqdm, pandas, seaborn, cpuinfo, filelock, packaging; from ultralytics import YOLO'
    if ($LASTEXITCODE -ne 0) {
      Write-Host 'Installing optional YOLO26 packages (first setup only)...'
      & $python -m pip install --disable-pip-version-check --timeout 30 --retries 2 -r (Join-Path $PSScriptRoot 'requirements-object-detection.txt')
      if ($LASTEXITCODE -eq 0) {
        & $python -m pip install --disable-pip-version-check --timeout 30 --retries 2 --no-deps 'ultralytics==8.4.183'
        if ($LASTEXITCODE -eq 0) { & $python -c 'from ultralytics import YOLO'; if ($LASTEXITCODE -ne 0) { $torchReady = $false } }
        else { $torchReady = $false }
      } else { $torchReady = $false }
    }
  }
  if (-not $torchReady) {
    Write-Warning 'YOLO26 object detection could not be prepared. The camera and face-enrollment app will still start; object detection will be unavailable until its optional dependencies are installed.'
  } elseif ($nvidiaSmi -and -not $cudaReady) {
    Write-Warning 'PyTorch is available, but CUDA is not. YOLO26 will use CPU; the camera and face-enrollment app can still run.'
  }

  $startupPhase = 'preparing the temporary camera tunnel'
  $cloudflared = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
  if (-not $cloudflared) { $cloudflared = Get-Command cloudflared -ErrorAction SilentlyContinue }
  if ($cloudflared) { $cloudflaredPath = $cloudflared.Source }
  else {
    $toolsDir = Join-Path $PSScriptRoot 'tools'
    New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
    $cloudflaredPath = Join-Path $toolsDir 'cloudflared.exe'
    $cloudflaredWorks = $false
    if (Test-Path -LiteralPath $cloudflaredPath) {
      & $cloudflaredPath version *> $null
      $cloudflaredWorks = $LASTEXITCODE -eq 0
    }
    if (-not $cloudflaredWorks) {
      Write-Host 'Downloading cloudflared from the official Cloudflare Windows release...'
      $cloudflaredTemp = "$cloudflaredPath.download-$PID"
      Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $cloudflaredTemp
      if ((Get-Item -LiteralPath $cloudflaredTemp).Length -lt 1000000) { Remove-Item -LiteralPath $cloudflaredTemp -Force -ErrorAction SilentlyContinue; throw 'The cloudflared download was incomplete. Check internet access and run this file again.' }
      & $cloudflaredTemp version *> $null
      if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $cloudflaredTemp -Force -ErrorAction SilentlyContinue; throw 'The downloaded cloudflared file is not a valid Windows executable.' }
      Move-Item -LiteralPath $cloudflaredTemp -Destination $cloudflaredPath -Force
    }
  }

  New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
  # Both local services inherit a per-run secret used only for private model telemetry.
  $env:VAOS_INTERNAL_STATUS_TOKEN = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))
  $startupPhase = 'launching the VAOS admin service'
  Start-AppServices
  $passwordFile = Join-Path $PSScriptRoot 'data\admin_password.txt'
  if (Test-Path -LiteralPath $passwordFile) {
    Write-Host "Dashboard sign-in: admin / $((Get-Content -LiteralPath $passwordFile -Raw).Trim())"
  }

  # Use TCP/HTTP2 because some networks block QUIC over UDP port 7844.
  $startupPhase = 'starting the temporary camera tunnel'
  Start-Tunnel

  Write-Host ''
  Write-Host 'Private admin dashboard: http://127.0.0.1:8000'
  Write-Host "Temporary camera URL: $publicUrl"
  Write-Host 'Only the camera service is public. Anyone with the temporary URL can reach its camera-link pages.'
  Write-Host 'The dashboard now fills in the camera URL automatically.'
  Write-Host 'Press Enter or Ctrl+C to stop the app and tunnel.'
  Start-Process 'http://127.0.0.1:8000'

  while ($true) {
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $launcherCommandPath) {
      try {
        $command = Get-Content -LiteralPath $launcherCommandPath -Raw | ConvertFrom-Json
      } catch { $command = $null }
      Remove-Item -LiteralPath $launcherCommandPath -Force -ErrorAction SilentlyContinue
      if ($command) {
        try { switch ([string]$command.command) {
          'services_restart' {
            Write-Host 'Restarting VAOS services while keeping the camera tunnel connected...'
            Stop-AppServices
            Start-AppServices
            Write-Host "VAOS services are back. Camera tunnel remains at $publicUrl"
          }
          'tunnel_start' { Start-Tunnel }
          'tunnel_stop' {
            Stop-Tunnel
            Set-TunnelState 'stopped' 'Camera tunnel is stopped.'
            Write-Host 'Camera tunnel stopped. VAOS local services are still running.'
          }
          'tunnel_restart' {
            Stop-Tunnel
            Set-TunnelState 'restarting' 'Restarting the temporary camera tunnel.'
            Start-Tunnel
          }
        } } catch { Write-Warning ("Launcher request failed: {0}" -f $_.Exception.Message) }
      }
    }
    if (($tunnel -and $tunnel.HasExited)) {
      Write-Warning 'The camera tunnel exited. Local services are still available; use Tunnel controls to start it again.'
      Set-TunnelState 'stopped' 'The camera tunnel exited. Use Tunnel controls to start it again.'
      $script:tunnel = $null
      Remove-Item -LiteralPath $publicUrlPath -Force -ErrorAction SilentlyContinue
    }
    if (($admin -and $admin.HasExited) -or ($camera -and $camera.HasExited)) {
      Write-Warning 'A local VAOS service exited. The launcher remains open and the camera tunnel stays connected.'
      Start-Sleep -Seconds 2
    }
    if ([Console]::KeyAvailable) {
      $key = [Console]::ReadKey($true)
      if ($key.Key -eq [ConsoleKey]::Enter) { break }
    }
  }
} catch {
  Write-Host ("Startup error while {0}: {1}: {2}" -f $startupPhase, $_.Exception.GetType().FullName, $_.Exception.Message) -ForegroundColor Red
  if ($_.ScriptStackTrace) { Write-Host ("PowerShell location: {0}" -f $_.ScriptStackTrace) -ForegroundColor DarkYellow }
  Write-Host ("Run logs: {0}" -f $logsDir) -ForegroundColor Yellow
  foreach ($errorLog in @($adminErrorLog,$cameraErrorLog,$tunnelErr)) {
    if (Test-Path -LiteralPath $errorLog) {
      $tail = Get-Content -LiteralPath $errorLog -Tail 30 -ErrorAction SilentlyContinue
      if ($tail) { Write-Host ("--- {0} ---" -f (Split-Path -Leaf $errorLog)) -ForegroundColor DarkYellow; $tail | ForEach-Object { Write-Host $_ } }
    }
  }
  Write-Host ("Full PowerShell exception: {0}" -f $_.Exception.ToString()) -ForegroundColor DarkYellow
  $exitCode = 1
} finally {
  Stop-LocalServices
  Remove-Item Env:VAOS_INTERNAL_STATUS_TOKEN -ErrorAction SilentlyContinue
  Write-Host 'Local services and the temporary tunnel have been stopped.'
}

if ($exitCode -eq 1) { exit 1 }
