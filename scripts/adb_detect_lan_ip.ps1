# Print this PC's LAN IP (the one the TestHub server must use to reach it).
#
# Called by adb_remote_server.bat. Output MUST be exactly one bare IP on a
# single line (or nothing) because the caller does `for /f ... do set LOCAL_IP=`
# - any log noise would end up inside the variable.
#
# Ordering rationale: the previous implementation in the .bat took the FIRST
# "IPv4" line from ipconfig, which on Docker / WSL / VMware / Hyper-V hosts is
# often a 172.x virtual adapter. That produced an adb_host the backend could
# never route to, so devices looked online and every execution failed.
# We therefore prefer a DHCP/private physical NIC and explicitly exclude
# virtual adapter names, falling back to the default-route interface.

function Get-LanIp {
    try {
        $cand = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object {
                $_.IPAddress -notlike '127.*' -and
                $_.IPAddress -notlike '169.254.*' -and
                $_.PrefixOrigin -ne 'WellKnown' -and
                $_.InterfaceAlias -notmatch 'Loopback|WSL|Hyper-V|VirtualBox|VMware|vEthernet|Docker|Tailscale|ZeroTier'
            } |
            Sort-Object -Property @{ Expression = { if ($_.PrefixOrigin -eq 'Dhcp') { 0 } else { 1 } } } |
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

$ip = Get-LanIp
if ($ip) { Write-Output $ip }
