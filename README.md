# Address Plan Automation

Standalone tool that generates a dual-stack (IPv4 + IPv6) CSV address plan defining all IP subnet allocations for a network site deployment.

## Folder Structure

```
address-plan-automation/
├── generate_address_plan.py      # Main script
├── config/
│   ├── vlans.yaml                # VLAN definitions, subnet sizing, IPv6 offsets
│   └── site_types.yaml           # Device roles, hardware models per site type
├── inputs/
│   ├── nyc_input.yaml            # Medium site example (New York)
│   └── bgl_input.yaml            # Small site example (Bangalore)
└── output/                       # Generated CSV output files
```

## Quick Start

```bash
cd address-plan-automation
python3 generate_address_plan.py inputs/nyc_input.yaml    # Medium site
python3 generate_address_plan.py inputs/bgl_input.yaml    # Small site
```

Output goes to `output/<site>-address-plan.csv`.

## Prerequisites

- Python 3.6+
- PyYAML (`pip install pyyaml`)

## What It Produces

The address plan CSV has 7 columns:

| Column | Content |
|--------|---------|
| **Subnet** | Section label (e.g., "nyc VLAN 100 Data") |
| **IPv4** | IPv4 subnet in CIDR notation |
| **IPv6** | IPv6 subnet in CIDR notation |
| *(separator)* | Empty column for visual separation |
| **DNS** | DNS hostname for the entry |
| **A** | IPv4 host address (A record) |
| **AAAA** | IPv6 host address (AAAA record) |

### CSV Sections

| Section | Description |
|---------|-------------|
| **INFRA block** | Summary of infrastructure address space (IPv4 + infra IPv6 /52) |
| **USER block** | Summary of user-facing address space (IPv4 + user IPv6 /48) |
| **Crosslinks** | Point-to-point /30 (IPv4) and /127 (IPv6) subnets between devices |
| **Loopbacks** | Management loopback IPs; IPv6 host embeds the IPv4 address |
| **VLAN 100 Data** | Corporate data subnet (/64 from user IPv6) |
| **VLAN 150 Voice** | Voice/telephony subnet (/64 from user IPv6) |
| **VLAN 200 Guest** | Guest access subnet (/64 from user IPv6) |
| **VLAN 300 Services** | Wireless AP management + services subnet (/64 from infra IPv6) |

## Input YAML

Key fields:

```yaml
site: nyc                              # Site identifier (used in device names)
site_description: "New York Office"
site_type: medium                      # small | medium

network:
  infra_block: "10.1.2.0/23"              # IPv4 infrastructure subnets
  user_block: "10.128.96.0/21"            # IPv4 user-facing subnets
  infra_ipv6: "2001:db8:c100::/52"        # IPv6 for crosslinks, loopbacks, services
  user_ipv6: "2001:db8:a100::/48"         # IPv6 for Data, Voice, Guest VLANs

services:
  lab: true                            # Enable lab gateway(s)
  voice: true                          # Enable voice gateway (medium only)

floors:
  - floor: "11"
    ap_count: 15
    switch_stacks:
      - stack_id: 1
        members: 2
```

## IPv6 Address Scheme

Two separate IPv6 prefixes per site:

### Infrastructure Prefix (infra_ipv6, /52)

| Hextet Offset | Prefix | Purpose |
|--------------|--------|---------|
| `+0` to `+3f` | /127 each | Crosslinks (4th hextet = IPv4 byte offset) |
| WAN offsets | /64 each | WAN transit links |
| `+0x100` | /64 | Loopbacks (host = embedded IPv4) |
| `+0x111` | /64 | VLAN 300 Services |

### User Prefix (user_ipv6, /48)

| VLAN ID | Hextet (hex) | Prefix | Purpose |
|---------|-------------|--------|---------|
| 100 | `0x64` | /64 | Data |
| 150 | `0x96` | /64 | Voice |
| 200 | `0xc8` | /64 | Guest |

### IPv6 Host Address Patterns

| Device Type | AAAA Pattern | Example |
|------------|-------------|---------|
| HSRP VIP | `::1` | `2001:db8:a100:64::1` |
| Core-sw1 SVI | `::2` | `2001:db8:a100:64::2` |
| Core-sw2 SVI | `::3` | `2001:db8:a100:64::3` |
| Floorwise access switch (Data) | Embedded IPv4 | `10.128.96.4` → `::a80:6004` |
| Loopback | Embedded IPv4 | `10.1.2.129` → `::a01:281` |
| Crosslink host A | `::` (first in /127) | `2001:db8:c100:4::` |
| Crosslink host B | `::1` (second in /127) | `2001:db8:c100:4::1` |
| APs | `N/A` | — |

## Site Types

### Small
- Single WAN gateway → access switch stack (no core layer)
- Single console server, optional lab gateway
- SVIs on the access switch

### Medium
- Dual WAN gateways with cross-connect
- Dual core switches with HSRP
- Dual-homed access stacks to both core switches
- Optional dual lab gateways and voice gateway

## Configuration Files

| File | Purpose |
|------|---------|
| `config/vlans.yaml` | VLAN definitions, IPv4 subnet sizing per site type, IPv6 hextet offsets |
| `config/site_types.yaml` | Device roles, hardware models, redundancy levels per site type |
