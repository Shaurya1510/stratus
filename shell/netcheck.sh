#!/usr/bin/env bash
# =============================================================================
# netcheck.sh — verify (and fix) the network conditions Stratus needs
# =============================================================================
# Stratus needs three things from the network: an address other machines can
# reach, an open control port, and UDP broadcast for discovery. When joining
# fails, it is almost always one of those three, and this script says which.
#
#   ./stratus.sh netcheck             report the network state
#   ./stratus.sh netcheck --fix       open the required firewall ports
#   ./stratus.sh netcheck --scan      look for Stratus servers on this network
# =============================================================================
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

CONTROL_PORT=5050
DISCOVERY_PORT=5051
FIX=0
SCAN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -f|--fix)  FIX=1; shift ;;
        -s|--scan) SCAN=1; shift ;;
        -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

require_linux
need ip iproute2

# The configured port overrides the default when a server exists.
if server_configured; then
    CONTROL_PORT="$(server_field port)"
    CONTROL_PORT="${CONTROL_PORT:-5050}"
fi

# =============================================================================
# 1. Interfaces and addresses
# =============================================================================
section "Network interfaces"

# `ip -brief addr` gives one compact line per interface:
#   wlp3s0  UP  192.168.1.7/24
# The awk filter drops loopback and interfaces with no address.
ip -brief addr show 2>/dev/null | awk '$1 != "lo" && $3 != "" {
    printf "  %-14s %-8s %s\n", $1, $2, $3
}' || warn "Could not list interfaces"

ADDRESS="$(lan_address)"
say ""
if [[ "$ADDRESS" == "127.0.0.1" ]]; then
    fail "No local network address — this machine is not on a network"
    note "Connect to Wi-Fi or plug in an Ethernet cable, then run this again."
else
    ok "Other devices should reach this host at: ${C_BOLD}$ADDRESS${C_RESET}"
fi

# The default route tells us which interface leads to the rest of the network.
gateway="$(ip route show default 2>/dev/null | awk 'NR == 1 { print $3 }')"
interface="$(ip route show default 2>/dev/null | awk 'NR == 1 { print $5 }')"
[[ -n "$gateway" ]] && note "Default gateway: $gateway via $interface" \
                    || warn "No default route — this is a local-only network"

# =============================================================================
# 2. Ports
# =============================================================================
section "Stratus ports"

if port_listening "$CONTROL_PORT"; then
    ok "Control port $CONTROL_PORT is listening"
    # Show what is bound to it. `ss -ltnp` includes the process name; it needs
    # privileges to show processes owned by other users, hence the fallback.
    ss -ltnp 2>/dev/null | awk -v port=":$CONTROL_PORT" '$4 ~ port"$" {
        print "    bound by:", $NF
    }' || true
else
    warn "Nothing is listening on control port $CONTROL_PORT"
    note "That is expected when the server is not running."
fi

if ss -lun 2>/dev/null | awk -v port=":$DISCOVERY_PORT" '$4 ~ port"$" { found = 1 } END { exit !found }'; then
    ok "Discovery port $DISCOVERY_PORT (UDP) is open"
else
    warn "Discovery port $DISCOVERY_PORT (UDP) is not open"
    note "Clients will need to type the address manually."
fi

# =============================================================================
# 3. Firewall
# =============================================================================
section "Firewall"

if have ufw; then
    status="$(ufw status 2>/dev/null | awk 'NR == 1 { print $2 }' || echo unknown)"
    if [[ "$status" != "active" ]]; then
        ok "ufw is inactive — nothing is being blocked"
    else
        info "ufw is active; checking the ports Stratus needs"
        allowed_tcp=0
        allowed_udp=0
        ufw status 2>/dev/null | grep -q "$CONTROL_PORT/tcp" && allowed_tcp=1
        ufw status 2>/dev/null | grep -q "$DISCOVERY_PORT/udp" && allowed_udp=1

        (( allowed_tcp )) && ok "TCP $CONTROL_PORT allowed" || fail "TCP $CONTROL_PORT is blocked"
        (( allowed_udp )) && ok "UDP $DISCOVERY_PORT allowed" || fail "UDP $DISCOVERY_PORT is blocked"

        if (( FIX )) && { (( ! allowed_tcp )) || (( ! allowed_udp )); }; then
            say ""
            info "Opening the required ports (this needs sudo)…"
            # Restrict the rules to private address ranges. Opening a port to
            # the whole world when the host is on a public network would expose
            # the server far beyond the classroom it was meant for.
            for subnet in 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12; do
                sudo ufw allow from "$subnet" to any port "$CONTROL_PORT" proto tcp >/dev/null 2>&1 || true
                sudo ufw allow from "$subnet" to any port "$DISCOVERY_PORT" proto udp >/dev/null 2>&1 || true
            done
            ok "Opened TCP $CONTROL_PORT and UDP $DISCOVERY_PORT for private networks only"
        elif (( ! allowed_tcp )) || (( ! allowed_udp )); then
            note "Run './stratus.sh netcheck --fix' to open them."
        fi
    fi
elif have firewall-cmd; then
    warn "firewalld detected — Stratus does not configure it automatically"
    note "Allow the ports with:"
    note "  sudo firewall-cmd --add-port=$CONTROL_PORT/tcp --permanent"
    note "  sudo firewall-cmd --add-port=$DISCOVERY_PORT/udp --permanent"
    note "  sudo firewall-cmd --reload"
else
    ok "No firewall manager found — ports are unlikely to be blocked"
fi

# =============================================================================
# 4. Client isolation
# =============================================================================
# Many campus and hotel access points enable "client isolation", which stops
# devices on the same Wi-Fi from talking to each other at all. Stratus cannot
# work through it, and the symptom (discovery finds nothing, manual IP times
# out) looks like a bug in the software. Checking the ARP table is a decent
# proxy: if this machine has never seen another device, isolation is likely.
# =============================================================================
section "Local network reachability"

neighbours="$(ip neigh show 2>/dev/null | awk '$0 !~ /FAILED|INCOMPLETE/ { count++ } END { print count + 0 }')"
if (( neighbours > 1 )); then
    ok "$neighbours other device(s) visible on this network"
elif (( neighbours == 1 )); then
    warn "Only the gateway is visible from here"
    note "If clients cannot join, the access point may have client isolation enabled."
    note "Try a wired connection or a phone hotspot to confirm."
else
    warn "No other devices visible on this network"
fi

# =============================================================================
# 5. Scan for servers
# =============================================================================
# Sends the same UDP discovery probe the client uses and prints who answers.
# Python is used for the socket work because plain bash cannot send UDP
# broadcasts portably.
# =============================================================================
if (( SCAN )); then
    section "Scanning for Stratus servers"
    info "Broadcasting on UDP $DISCOVERY_PORT…"

    python3 - "$DISCOVERY_PORT" <<'PYTHON'
import json, socket, sys, time

port = int(sys.argv[1])
probe = b"STRATUS_DISCOVER_V1"

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.settimeout(0.3)
try:
    sock.sendto(probe, ("255.255.255.255", port))
except OSError as error:
    print(f"  broadcast failed: {error}")
    raise SystemExit(1)

found = {}
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    try:
        data, address = sock.recvfrom(4096)
    except socket.timeout:
        continue
    try:
        item = json.loads(data)
        found[address[0]] = item
    except ValueError:
        continue
sock.close()

if not found:
    print("  no servers answered")
    raise SystemExit(0)

for host, item in sorted(found.items()):
    print(f"  {item.get('name', 'unnamed'):<30} {host}:{item.get('control_port', '?')}")
PYTHON

    say ""
    note "Join one with: ./stratus.sh terminal"
fi

# =============================================================================
# Summary
# =============================================================================
section "Summary"
say "  Host address     : ${C_BOLD}$ADDRESS${C_RESET}"
say "  Control port     : $CONTROL_PORT (TCP)"
say "  Discovery port   : $DISCOVERY_PORT (UDP)"
say ""
if [[ "$ADDRESS" != "127.0.0.1" ]]; then
    note "Members on this network join by running: ./stratus.sh terminal"
    note "Hosted applications will be reachable at http://$ADDRESS:<port>"
fi
