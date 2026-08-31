"""Which address other devices on this network should use for this host."""
from __future__ import annotations

import socket


def lan_address(fallback: str = "127.0.0.1") -> str:
    """The address of the interface that reaches the local network.

    Opening a UDP socket toward an off-link address makes the kernel choose an
    outgoing interface without sending a single packet, which works whether the
    host is on Wi-Fi, Ethernet or a phone hotspot. A classroom host is often
    offline, so any failure falls back to the loopback address.
    """
    for probe in ("192.168.1.1", "10.255.255.255", "1.1.1.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.2)
            sock.connect((probe, 9))
            address = sock.getsockname()[0]
            if address and not address.startswith("127."): return address
        except OSError: continue
        finally: sock.close()
    try:
        address = socket.gethostbyname(socket.gethostname())
        if address and not address.startswith("127."): return address
    except OSError: pass
    return fallback


def local_subnet(fallback: str = "192.168.1.0/24") -> str:
    """The CIDR block this host sits in, for suggesting a sensible default.

    A /24 is assumed because that is what essentially every home router, phone
    hotspot and small campus network hands out. It is a suggestion the host can
    edit, not a claim about the real netmask.
    """
    address = lan_address()
    if address.startswith("127."): return fallback
    octets = address.split(".")
    if len(octets) != 4: return fallback
    return f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"


def parse_networks(values) -> list:
    """Turn host-supplied text into networks, rejecting anything malformed.

    Accepts a plain address ("192.168.1.50"), a CIDR block
    ("192.168.1.0/24") or a range ("192.168.1.50-192.168.1.80"). A range is
    expanded into the smallest set of CIDR blocks that covers it, because that
    is what the standard library can match against efficiently.
    """
    import ipaddress

    if isinstance(values, str): values = [values]
    networks = []
    for raw in values or []:
        text = str(raw).strip()
        if not text: continue
        try:
            if "-" in text:
                first, last = (part.strip() for part in text.split("-", 1))
                networks.extend(ipaddress.summarize_address_range(
                    ipaddress.ip_address(first), ipaddress.ip_address(last)))
            else:
                networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError as error:
            raise ValueError(f"'{text}' is not a valid address, range or network: {error}") from None
    return networks


def address_allowed(address: str, networks) -> bool:
    """May a client at this address join?

    An empty list means no restriction. The loopback address is always allowed
    no matter what the host configures, because the administration tools connect
    over it — a typo in a range must never lock the owner out of their own
    server.
    """
    import ipaddress

    if not networks: return True
    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    if candidate.is_loopback: return True
    return any(candidate in network for network in networks)


def describe_networks(networks) -> str:
    if not networks: return "any device on this network"
    return ", ".join(str(network) for network in networks)


def local_addresses() -> set:
    """Every address that belongs to this machine.

    Used to decide whether a service is running on the same computer as the
    browser about to open it. That matters because browsers only expose the
    WebCrypto API in a "secure context": HTTPS, or a loopback address. Reaching
    a local service through its LAN address gives a page with no crypto support
    at all, while 127.0.0.1 works with no certificate.
    """
    import socket
    addresses = {"127.0.0.1", "::1", "localhost"}
    try:
        hostname = socket.gethostname()
        addresses.add(hostname)
        for info in socket.getaddrinfo(hostname, None):
            addresses.add(info[4][0])
    except OSError:
        pass
    addresses.add(lan_address())
    return addresses


def is_local(address: str) -> bool:
    return bool(address) and address in local_addresses()


def browser_host(address: str) -> str:
    """The address a browser on this machine should use to reach a service.

    Loopback is preferred whenever the service is local, because it is a secure
    context and therefore the only way WebCrypto works without a certificate.
    """
    return "127.0.0.1" if is_local(address) else address


def local_interfaces() -> list[dict]:
    """Every usable IPv4 network this machine is attached to.

    Returns one entry per address with its interface name, its address, the
    prefix length, and the broadcast address for that subnet.

    `ip -json addr` is asked first because it is exact and iproute2 is already
    a requirement. Its JSON output was added in iproute2 4.13 (2017); anything
    older falls back to reading the plain text, and a machine where neither
    works still gets the single address the routing table implies.
    """
    import ipaddress
    import json
    import subprocess

    found: list[dict] = []
    seen: set[str] = set()

    def remember(name: str, address: str, prefix: int, broadcast: str = "") -> None:
        if not address or address in seen: return
        try:
            interface = ipaddress.ip_interface(f"{address}/{prefix}")
        except ValueError:
            return
        if interface.ip.is_loopback or interface.ip.is_link_local: return
        seen.add(address)
        found.append({"interface": name, "address": address, "prefix": prefix,
                      "broadcast": broadcast or str(interface.network.broadcast_address),
                      "network": str(interface.network)})

    try:
        result = subprocess.run(["ip", "-json", "-4", "addr", "show"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            # `ip` is expected to print a list of interfaces. Anything else —
            # an object, a string, a wrapper's error report — is not something
            # to iterate over, and iterating it anyway raised an exception the
            # caller had no reason to expect.
            entries = json.loads(result.stdout or "[]")
            if not isinstance(entries, list): entries = []
            for entry in entries:
                if not isinstance(entry, dict): continue
                # A cable that is unplugged still has an address; sending on it
                # wastes a second of the scan and can never be answered.
                if entry.get("operstate") == "DOWN": continue
                for info in entry.get("addr_info") or []:
                    if not isinstance(info, dict): continue
                    if info.get("family") != "inet": continue
                    try: prefix = int(info.get("prefixlen", 24))
                    except (TypeError, ValueError): prefix = 24
                    remember(entry.get("ifname", "?"), info.get("local", ""),
                             prefix, info.get("broadcast") or "")
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        pass

    if not found:
        try:
            result = subprocess.run(["ip", "-4", "addr", "show"], capture_output=True, text=True, timeout=5)
            name = "?"
            for line in (result.stdout or "").splitlines():
                stripped = line.strip()
                if not line.startswith(" ") and ": " in line:
                    name = line.split(": ", 2)[1].split("@")[0]
                elif stripped.startswith("inet "):
                    parts = stripped.split()
                    address, _, prefix = parts[1].partition("/")
                    broadcast = parts[parts.index("brd") + 1] if "brd" in parts else ""
                    remember(name, address, int(prefix or 24), broadcast)
        except (OSError, ValueError, TypeError, AttributeError,
                subprocess.SubprocessError, IndexError):
            pass

    if not found:
        remember("default", lan_address(), 24)

    return found


def same_network(first: str, second: str, prefix: int = 24) -> bool:
    """Whether two addresses could reach each other without a router.

    Used to explain the commonest reason two people cannot see each other: the
    virtual machine is on its own private NAT network rather than on the Wi-Fi
    the other person is using, so no amount of broadcasting will help.
    """
    import ipaddress

    try:
        one = ipaddress.ip_interface(f"{first}/{prefix}")
        two = ipaddress.ip_address(second)
    except ValueError:
        return False
    return two in one.network
