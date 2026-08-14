[CmdletBinding()]
param(
    [ValidateSet("probe", "auth", "bootstrap", "exec")]
    [string]$Mode = "probe",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemoteCommand
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "dgx_ssh.sh"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Missing WSL wrapper: $scriptPath"
}

$wslInputPath = $scriptPath.Replace('\', '/')
$wslScriptPath = (& wsl.exe -d Ubuntu -- wslpath -a $wslInputPath).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($wslScriptPath)) {
    throw "Unable to resolve the WSL path for $scriptPath"
}

$dgxTarget = "cwliao@140.96.58.171"
$sshExe = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"

function Get-NativeSshOptions {
    $options = @(
        "-o", "ConnectTimeout=8",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "AddKeysToAgent=yes"
    )
    $identity = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
    if (Test-Path -LiteralPath $identity -PathType Leaf) {
        $options += @("-i", $identity)
    }
    return $options
}

function Get-SshFailureStatus {
    param(
        [object[]]$Output,
        [int]$Status
    )
    $text = ($Output -join "`n")
    if ($text -match '(?i)host key verification failed|remote host identification has changed|offending .* key|no .* host key is known') {
        return $Status
    }
    if ($text -match '(?i)permission denied|no supported authentication methods|too many authentication failures|sign_and_send_pubkey.*failed') {
        return 75
    }
    return $Status
}

function Invoke-NativeProbe {
    if (-not (Test-Path -LiteralPath $sshExe -PathType Leaf)) {
        return 127
    }
    $output = & $sshExe @(Get-NativeSshOptions) "-o" "BatchMode=yes" $dgxTarget "printf 'SSH_OK\n'; hostname; id -un" 2>&1
    $status = $LASTEXITCODE
    $output
    return (Get-SshFailureStatus -Output $output -Status $status)
}

function Invoke-NativeExec {
    if (-not (Test-Path -LiteralPath $sshExe -PathType Leaf)) {
        return 127
    }
    if ($RemoteCommand.Count -eq 0) {
        Write-Error "USAGE: dgx_ssh.ps1 exec <remote-command>"
        return 64
    }
    $remote = $RemoteCommand -join " "
    & $sshExe @(Get-NativeSshOptions) "-o" "BatchMode=yes" $dgxTarget $remote
    return $LASTEXITCODE
}

function Invoke-NativeAuth {
    if (-not (Test-Path -LiteralPath $sshExe -PathType Leaf)) {
        return 127
    }
    $options = @(Get-NativeSshOptions) + @("-o", "BatchMode=no")
    if ($RemoteCommand.Count -gt 0) {
        & $sshExe $options $dgxTarget ($RemoteCommand -join " ")
    } else {
        & $sshExe $options $dgxTarget
    }
    $status = $LASTEXITCODE
    if ($status -eq 0) {
        Write-Output "AUTH_OK: native Windows SSH route"
    }
    return $status
}

switch ($Mode) {
    "probe" {
        $wslOutput = & wsl.exe -d Ubuntu -- bash $wslScriptPath probe 2>&1
        $wslStatus = $LASTEXITCODE
        if ($wslStatus -eq 0) {
            $wslOutput
            exit 0
        }
        if ($wslStatus -ne 75) {
            $wslOutput
            exit $wslStatus
        }
        $nativeStatus = Invoke-NativeProbe
        if ($nativeStatus -eq 0) {
            exit 0
        }
        if ($nativeStatus -ne 75) {
            exit $nativeStatus
        }
        Write-Error "REAUTH_REQUIRED: both WSL and native Windows SSH routes failed"
        exit 75
    }
    "exec" {
        $wslOutput = & wsl.exe -d Ubuntu -- bash $wslScriptPath exec @RemoteCommand 2>&1
        $wslStatus = $LASTEXITCODE
        if ($wslStatus -eq 0) {
            $wslOutput
            exit 0
        }
        if ($wslStatus -ne 75) {
            $wslOutput
            exit $wslStatus
        }
        $nativeProbeStatus = Invoke-NativeProbe
        if ($nativeProbeStatus -ne 0) {
            if ($nativeProbeStatus -ne 75) {
                exit $nativeProbeStatus
            }
            Write-Error "REAUTH_REQUIRED: both WSL and native Windows SSH routes failed"
            exit 75
        }
        $nativeStatus = Invoke-NativeExec
        exit $nativeStatus
    }
    "auth" {
        & wsl.exe -d Ubuntu -- bash $wslScriptPath auth @RemoteCommand
        $wslStatus = $LASTEXITCODE
        if ($wslStatus -eq 0) {
            exit 0
        }
        if ($wslStatus -ne 75) {
            exit $wslStatus
        }
        $nativeStatus = Invoke-NativeAuth
        if ($nativeStatus -eq 0) {
            exit 0
        }
        Write-Error "REAUTH_REQUIRED: interactive auth failed on both WSL and native Windows SSH"
        exit 75
    }
    "bootstrap" {
        # Key bootstrap remains canonical in WSL; native Windows auth is still
        # available through `auth` and `exec` fallback without duplicating the
        # public-key installation policy here.
        & wsl.exe -d Ubuntu -- bash $wslScriptPath bootstrap @RemoteCommand
        exit $LASTEXITCODE
    }
}
