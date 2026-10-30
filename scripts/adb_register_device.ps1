param(
    [string]$ServerUrl = '',
    [string]$Udid = '',
    [string]$AdbHost = '',
    [int]$AdbPort = 5037,
    [string]$Token = '',
    [string]$Status = 'online',
    [string]$Name = ''
)

# Register / refresh an Android device on TestHub.
# Called by adb_remote_server.bat right after the remote adb server is up
# (Status=online) and on stop (Status=offline).
#
# WHY THIS EXISTS
# ---------------
# iOS devices were already auto-registered by ios_register_device.ps1, so a
# plugged-in iPhone just appeared in the TestHub device list. Android only had
# adb_remote_server.bat, which OPENS the adb server but never told the platform
# the device exists - users had to click "Scan devices" by hand. This script
# closes that asymmetry.
#
# PUBLIC / SHARED PLATFORM CONTRACT (important)
# ---------------------------------------------
# TestHub is a shared test platform and this device may be plugged into ANY
# PC on the LAN. So:
#   * the UDID is the identity  -> re-registering from another PC refreshes the
#     SAME row (status + latest host IP) instead of creating a duplicate;
#   * the AdbHost sent up MUST be an address the TestHub server can reach -
#     never 127.0.0.1 / localhost, because those would resolve to the *server*
#     itself (or to the backend container), not to the PC that owns the phone.
# The backend enforces the same rule, but we refuse early here so the failure is
# obvious at plug-in time rather than at execution time.
#
# ASCII-ONLY OUTPUT ON PURPOSE: the caller is a .bat under a legacy codepage.

function Get-LanIp {
    # Same strategy as ios_register_device.ps1: virtual adapters (WSL / Docker /
    # VMware / Hyper-V) routinely win a naive "ipconfig | first IPv4" race and
    # then the platform stores an address it cannot route to.
    try {
        $cand = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object {
                $_.IPAddress -notlike '127.*' -and
                $_.IPAddress -notlike '169.254.*' -and
                $_.PrefixOrigin -ne 'WellKnown' -and
                $_.InterfaceAlias -notmatch 'Loopback|WSL|Hyper-V|VirtualBox|VMware|vEthernet|Docker'
            } |
            Sort-Object -Property @{Expression = { if ($_.PrefixOrigin -eq 'Dhcp') { 0 } else { 1 } } } |
            Select-Object -First 1
        if ($cand) { return $cand.IPAddress }
    }
    catch { }

    try {
        $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
            Sort-Object RouteMetric | Select-Object -First 1
        if ($route) {
            $ip = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction Stop |
                Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
                Select-Object -First 1
            if ($ip) { return $ip.IPAddress }
        }
    }
    catch { }

    try {
        $lines = (& ipconfig) -join "`n"
        $m = [regex]::Match($lines, 'IPv4[^:]*:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)')
        if ($m.Success) { return $m.Groups[1].Value }
    }
    catch { }

    return ''
}

function Test-LoopbackHost([string]$h) {
    if (-not $h) { return $true }
    $bare = $h
    if ($bare -like '*:*') { $bare = $bare.Split(':')[0] }
    return ($bare -eq '127.0.0.1' -or $bare -eq 'localhost' -or $bare -eq '0.0.0.0' -or $bare -eq '::1')
}

if (-not $ServerUrl) {
    Write-Host '  [skip] TESTHUB_URL is empty (device NOT registered)'
    exit 0
}
if (-not $Udid) {
    Write-Host '  [skip] no UDID, device NOT registered'
    exit 0
}

$isOnline = ($Status -ne 'offline')

# ---- resolve the host part (the PC that owns the phone) ----
$hostOnly = ''
if ($AdbHost) {
    if ($AdbHost -like '*:*') {
        $hostOnly = $AdbHost.Split(':')[0]
        $p = 0
        if ([int]::TryParse($AdbHost.Split(':')[1], [ref]$p) -and $p -gt 0) { $AdbPort = $p }
    }
    else {
        $hostOnly = $AdbHost
    }
}

if ($isOnline) {
    if (-not $hostOnly -or (Test-LoopbackHost $hostOnly)) {
        $resolved = Get-LanIp
        if ($resolved) {
            if ($hostOnly) {
                Write-Host ('  [info] caller supplied loopback (' + $hostOnly + '); using LAN IP ' + $resolved)
            }
            else {
                Write-Host ('  [info] caller did not supply the LAN IP, resolved locally: ' + $resolved)
            }
            $hostOnly = $resolved
        }
    }
}

# A loopback address is meaningless on a shared platform: it makes the device
# look online while every other user's execution silently fails.
if ($isOnline -and (Test-LoopbackHost $hostOnly)) {
    Write-Host '  [WARN] could not determine a routable LAN IP for this PC.'
    Write-Host '         Refusing to register with a loopback address (other users'
    Write-Host '         would see the device online but execution would fail).'
    Write-Host '         Fix: make sure this PC is on the same LAN as TestHub, then rerun.'
    exit 0
}

$finalAdbHost = ''
if ($isOnline) {
    $finalAdbHost = '' + $hostOnly + ':' + $AdbPort
}

# ---- best-effort: read model / Android version straight from the phone ----
$model = ''
$androidVersion = ''
if ($isOnline) {
    try {
        $model = (& adb -s $Udid shell getprop ro.product.model 2>$null | Out-String).Trim()
        $androidVersion = (& adb -s $Udid shell getprop ro.build.version.release 2>$null | Out-String).Trim()
        # getprop returns CRLF on some OEM builds; strip anything non-printable
        $model = ($model -replace '[^\x20-\x7E]', '').Trim()
        $androidVersion = ($androidVersion -replace '[^\x20-\x7E]', '').Trim()
    }
    catch {
        Write-Host '  [warn] could not read device properties, registering anyway'
    }
}

if (-not $Name) {
    if ($model) { $Name = $model + ' (' + $Udid + ')' }
    else { $Name = 'Android (' + $Udid + ')' }
}

$body = @{
    platform    = 'android'
    udid        = $Udid
    device_type = 'real'
    status      = $Status
    name        = $Name
}
if ($isOnline) {
    $body.adb_host = $finalAdbHost
    $body.platform_version = "$androidVersion"
    $body.notes = 'auto-registered by adb_remote_server.bat'
}
else {
    # Offline report comes from a PC that may no longer host the phone, so it
    # must NOT carry an address (same rule as the iOS script).
    $body.notes = 'marked offline by adb_remote_server.bat'
}
$body = $body | ConvertTo-Json

$headers = @{}
if ($Token) { $headers['X-Device-Token'] = $Token }

$base = $ServerUrl.TrimEnd('/')
try {
    $r = Invoke-RestMethod -Uri "$base/api/ui-automation/device-agent/register/" `
        -Method Post -Body $body -ContentType 'application/json' `
        -Headers $headers -TimeoutSec 15
    if ($r.success) {
        $action = 'updated'
        if ($r.created) { $action = 'CREATED new' }
        elseif (-not $r.online) { $action = 'marked OFFLINE' }
        Write-Host ('  [OK] ' + $action + ' device on TestHub  (id=' + $r.data.id + ', status=' + $r.data.status + ')')
        Write-Host ('       name = ' + $r.data.name)
        Write-Host ('       udid = ' + $r.data.udid)
        if ($isOnline) {
            Write-Host ('       adb  = ' + $finalAdbHost)
            Write-Host '       NOTE: reachable by EVERY user of this platform, not just this PC.'
        }
    }
    else {
        Write-Host ('  [WARN] register rejected: ' + $r.message)
    }
}
catch {
    Write-Host ('  [WARN] register failed: ' + $_.Exception.Message)
    Write-Host '        (adb server is up; add the device manually in TestHub if needed)'
}
exit 0
