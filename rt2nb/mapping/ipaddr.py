"""RackTables IP storage -> canonical string forms.

RackTables stores:
  * IPv4 addresses/networks as a 32-bit integer (``ip`` column).  Depending on
    the MySQL build it can come back signed, so we always mask to 32 bits.
  * IPv6 addresses/networks as a 16-byte binary blob (``ip`` VARBINARY(16)).
"""

from __future__ import annotations

import ipaddress
from typing import Optional, Union


def ipv4_int_to_str(value: Union[int, None]) -> Optional[str]:
    if value is None:
        return None
    return str(ipaddress.IPv4Address(int(value) & 0xFFFFFFFF))


def ipv6_bytes_to_str(value: Union[bytes, bytearray, memoryview, str, None]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, str):
        # Some drivers hand back a str for VARBINARY; encode latin-1 to bytes.
        value = value.encode("latin-1")
    if len(value) != 16:
        return None
    return str(ipaddress.IPv6Address(bytes(value)))


def ipv4_cidr(ip_int: int, mask: int) -> Optional[str]:
    addr = ipv4_int_to_str(ip_int)
    if addr is None:
        return None
    try:
        net = ipaddress.ip_network(f"{addr}/{int(mask)}", strict=False)
    except ValueError:
        return None
    return str(net)


def ipv6_cidr(ip_bytes, mask: int) -> Optional[str]:
    addr = ipv6_bytes_to_str(ip_bytes)
    if addr is None:
        return None
    try:
        net = ipaddress.ip_network(f"{addr}/{int(mask)}", strict=False)
    except ValueError:
        return None
    return str(net)


def with_prefixlen(addr: str, prefixlen: int) -> str:
    """Return ``addr/prefixlen`` for a NetBox IPAddress value."""
    return f"{addr}/{int(prefixlen)}"
