"""RackTables Port type -> NetBox interface ``type`` slug.

RackTables describes a port with two dictionary fields:
  * inner interface (``iif_id`` -> PortInnerInterface, e.g. "hardwired", "SFP")
  * outer interface / connector (``oif_id`` -> PortOuterInterface, e.g.
    "1000Base-T", "10GBase-SR", "SFP+").

NetBox has a single ``type`` slug per interface.  We resolve primarily from the
outer-interface label (it carries the media/speed), falling back to the inner
label, and finally to ``other`` with a warning.  Config
``mapping.interface_type_overrides`` can override any label.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

OTHER = "other"

# Ordered (substring, netbox_type) rules, matched case-insensitively against the
# outer-interface label with spaces removed.  First match wins, so put the more
# specific/faster media first.
_RULES: Tuple[Tuple[str, str], ...] = (
    # copper twisted pair
    ("100base-tx", "100base-tx"),
    ("100base-t", "100base-tx"),
    ("2.5gbase-t", "2.5gbase-t"),
    ("5gbase-t", "5gbase-t"),
    ("10gbase-t", "10gbase-t"),
    ("1000base-t", "1000base-t"),
    ("10base-t", "100base-tx"),
    # 10G optical / DAC
    ("10gbase-sr", "10gbase-x-sfpp"),
    ("10gbase-lr", "10gbase-x-sfpp"),
    ("10gbase-er", "10gbase-x-sfpp"),
    ("10gbase-cx4", "10gbase-cx4"),
    ("10gbase-x", "10gbase-x-sfpp"),
    ("xfp", "10gbase-x-xfp"),
    ("xenpak", "10gbase-x-xenpak"),
    # 25/40/100G
    ("25gbase", "25gbase-x-sfp28"),
    ("sfp28", "25gbase-x-sfp28"),
    ("40gbase", "40gbase-x-qsfpp"),
    ("qsfp+", "40gbase-x-qsfpp"),
    ("qsfp28", "100gbase-x-qsfp28"),
    ("100gbase", "100gbase-x-qsfp28"),
    # 1G optical
    ("1000base-lx", "1000base-x-sfp"),
    ("1000base-sx", "1000base-x-sfp"),
    ("1000base-x", "1000base-x-sfp"),
    ("1000base-lh", "1000base-x-sfp"),
    ("gbic", "1000base-x-gbic"),
    # generic pluggable cages (no media -> best guess by cage)
    ("sfp+", "10gbase-x-sfpp"),
    ("sfp-100", "1000base-x-sfp"),
    ("sfp", "1000base-x-sfp"),
    ("qsfp-dd", "400gbase-x-qsfpdd"),
    ("qsfp", "40gbase-x-qsfpp"),
    # WAN / TDM
    ("t1", "t1"),
    ("e1", "e1"),
    ("t3", "t3"),
    ("e3", "e3"),
    # wireless
    ("802.11ac", "ieee802.11ac"),
    ("802.11ax", "ieee802.11ax"),
    ("802.11n", "ieee802.11n"),
    ("802.11", "ieee802.11a"),
    # serial / management / misc -> other, but recognised so we don't warn
    ("rj-45", "1000base-t"),
    ("rj45", "1000base-t"),
    ("hardwired", "other"),
    ("kvm", "other"),
    ("usb", "other"),
    ("console", "other"),
)

# Inner-interface fallbacks (used when the outer label yields nothing useful).
_INNER_RULES: Tuple[Tuple[str, str], ...] = (
    ("sfp+", "10gbase-x-sfpp"),
    ("sfp", "1000base-x-sfp"),
    ("qsfp+", "40gbase-x-qsfpp"),
    ("qsfp", "40gbase-x-qsfpp"),
    ("xfp", "10gbase-x-xfp"),
    ("gbic", "1000base-x-gbic"),
    ("hardwired", "1000base-t"),
)


def _match(label: str, rules) -> Optional[str]:
    if not label:
        return None
    key = label.strip().lower().replace(" ", "")
    for needle, nb_type in rules:
        if needle in key:
            return nb_type
    return None


def map_interface_type(
    outer_label: Optional[str],
    inner_label: Optional[str] = None,
    overrides: Optional[Dict[str, str]] = None,
) -> Tuple[str, bool]:
    """Return ``(netbox_type_slug, is_fallback)``.

    ``is_fallback`` is True when we could not confidently resolve the media and
    used ``other`` (the caller should emit a warning / log the original label).
    """
    overrides = overrides or {}
    for label in (outer_label, inner_label):
        if label and label in overrides:
            return overrides[label], False
        if label:
            low = label.strip().lower()
            if low in overrides:
                return overrides[low], False

    hit = _match(outer_label, _RULES)
    if hit and hit != OTHER:
        return hit, False
    inner_hit = _match(inner_label, _INNER_RULES)
    if inner_hit:
        return inner_hit, False
    # Nothing matched confidently (or only a "recognised -> other" rule hit):
    # fall back to "other" and signal the caller to log the original label.
    return OTHER, True
