#!/usr/bin/env python3
"""
Address Plan Generator

Generates a CSV address plan for a site based on YAML input configuration.
Supports 'small' and 'medium' site types with appropriate subnet allocations.
Generates both IPv4 and IPv6 address assignments.

Usage:
    python3 generate_address_plan.py inputs/bgl_input.yaml
    python3 generate_address_plan.py inputs/nyc_input.yaml
"""

import csv
import ipaddress
import os
import sys

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_yaml(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_config():
    """Load VLAN and site type configuration."""
    vlans_cfg = load_yaml(os.path.join(SCRIPT_DIR, "config", "vlans.yaml"))
    site_types_cfg = load_yaml(os.path.join(SCRIPT_DIR, "config", "site_types.yaml"))
    return vlans_cfg, site_types_cfg


def device_name(site, role, dev_id=1):
    """Generate generic device name: site-role-id."""
    role_map = {
        "wan_gw": "wan-gw",
        "core_sw": "core-sw",
        "console_server": "console-server",
        "lab_gw": "lab-gw",
        "voice_gw": "voice-gw",
    }
    return f"{site}-{role_map.get(role, role)}{dev_id}"


def floor_sw_name(site, floor, stack_id):
    """Generate floor switch name: site-floor-sw{stack_id}."""
    return f"{site}-{floor}-sw{stack_id}"


def ap_name(site, floor, ap_number):
    """Generate AP name: site-floor-cap{number}."""
    return f"{site}-{floor}-cap{ap_number}"


class AddressPlan:
    def __init__(self, input_cfg):
        self.cfg = input_cfg
        self.site = input_cfg["site"]
        self.site_type = input_cfg["site_type"]
        self.vlans_cfg, self.site_types_cfg = load_config()
        self.subnet_sizes = self.vlans_cfg["subnet_allocation"][self.site_type]

        self.infra_net = ipaddress.IPv4Network(input_cfg["network"]["infra_block"])
        self.user_net = ipaddress.IPv4Network(input_cfg["network"]["user_block"])

        # IPv6 dual-prefix support (infra + user)
        net = input_cfg.get("network", {})
        infra_v6 = net.get("infra_ipv6")
        user_v6 = net.get("user_ipv6")
        if infra_v6 and user_v6:
            self.infra_v6 = ipaddress.IPv6Network(infra_v6)
            self.user_v6 = ipaddress.IPv6Network(user_v6)
            self.ipv6_enabled = True
            self.v6_offsets = self.vlans_cfg.get("ipv6_offsets", {})
        else:
            self.infra_v6 = None
            self.user_v6 = None
            self.ipv6_enabled = False
            self.v6_offsets = {}

        self.rows = []
        self.crosslink_offset = 0

    def _row(self, subnet="", ipv4="", ipv6="", sep="", dns="", a_rec="", aaaa=""):
        self.rows.append([subnet, ipv4, ipv6, sep, dns, a_rec, aaaa])

    def _blank(self):
        self._row()

    def _ipv4_to_v6_host(self, ipv4_str):
        """Embed an IPv4 address into the lower 32 bits of an IPv6 host portion.

        Example: 10.15.26.129 -> octets [10, 15, 26, 129]
                 -> 0x0a0f1a81 -> ::a0f:1a81
        """
        octets = [int(o) for o in ipv4_str.split(".")]
        embedded = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
        return embedded

    def _infra_v6_crosslink(self, ipv4_offset):
        """Get IPv6 /127 for a crosslink based on its IPv4 byte offset.

        The IPv6 crosslink subnet uses the IPv4 offset as the 4th hextet.
        E.g., IPv4 .0/30 -> :0::/127, IPv4 .4/30 -> :4::/127, IPv4 .8/30 -> :8::/127
        """
        if not self.ipv6_enabled:
            return None, "", ""
        base = int(self.infra_v6.network_address)
        subnet_addr = ipaddress.IPv6Address(base + (ipv4_offset << 64))
        subnet = ipaddress.IPv6Network(f"{subnet_addr}/127", strict=False)
        hosts = list(subnet)
        return subnet, str(hosts[0]), str(hosts[1])

    def _infra_v6_wan_transit(self, ipv4_offset):
        """Get IPv6 /64 for a WAN transit link (mirrors IPv4 offset as hextet)."""
        if not self.ipv6_enabled:
            return None
        base = int(self.infra_v6.network_address)
        subnet_addr = ipaddress.IPv6Address(base + (ipv4_offset << 64))
        return ipaddress.IPv6Network(f"{subnet_addr}/64", strict=False)

    def _infra_v6_loopback_net(self):
        """Get the /64 loopback subnet from infra IPv6 prefix."""
        if not self.ipv6_enabled:
            return None
        hextet = self.v6_offsets.get("loopbacks", 0x100)
        base = int(self.infra_v6.network_address)
        addr = ipaddress.IPv6Address(base + (hextet << 64))
        return ipaddress.IPv6Network(f"{addr}/64", strict=False)

    def _infra_v6_services_net(self):
        """Get the /64 services VLAN subnet from infra IPv6 prefix."""
        if not self.ipv6_enabled:
            return None
        hextet = self.v6_offsets.get("services", 0x111)
        base = int(self.infra_v6.network_address)
        addr = ipaddress.IPv6Address(base + (hextet << 64))
        return ipaddress.IPv6Network(f"{addr}/64", strict=False)

    def _user_v6_vlan_net(self, vlan_id):
        """Get the /64 user VLAN subnet. 4th hextet = VLAN ID in decimal."""
        if not self.ipv6_enabled:
            return None
        base = int(self.user_v6.network_address)
        addr = ipaddress.IPv6Address(base + (int(vlan_id) << 64))
        return ipaddress.IPv6Network(f"{addr}/64", strict=False)

    def _next_crosslink(self):
        """Allocate next /30 from the IPv4 crosslink pool."""
        base = int(self.crosslink_base.network_address) + self.crosslink_offset
        subnet = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(base)}/30", strict=False)
        self.crosslink_offset += 4
        return subnet

    def _crosslink_entry(self, label, dev_a, port_a, dev_b, port_b):
        """Add a crosslink with two host entries (IPv4 + IPv6).

        IPv6 /127 has its 4th hextet mirroring the IPv4 byte offset.
        """
        ipv4_offset = self.crosslink_offset
        subnet = self._next_crosslink()
        hosts = list(subnet.hosts())
        ip_a, ip_b = str(hosts[0]), str(hosts[1])
        dns_a = f"{dev_a}-{port_a.replace('/', '-')}"
        dns_b = f"{dev_b}-{port_b.replace('/', '-')}"

        v6_sub, v6_a, v6_b = self._infra_v6_crosslink(ipv4_offset)
        v6_str = str(v6_sub) if v6_sub else ""

        self._row(label, str(subnet), v6_str, "", dns_a, ip_a, v6_a)
        self._row("", "", "", "", dns_b, ip_b, v6_b)
        self._blank()

    def generate(self):
        """Generate the complete address plan."""
        site = self.site
        site_upper = site.upper()
        desc = self.cfg.get("site_description", site_upper)

        # Header
        self._row("Subnet", "IPv4", "IPv6", "", "DNS", "A", "AAAA")
        self._row("Host Lock:", "False", "", "", "", "", "")

        # Summary blocks
        type_label = "Small" if self.site_type == "small" else "Medium"
        infra_v6_str = str(self.infra_v6) if self.ipv6_enabled else ""
        user_v6_str = str(self.user_v6) if self.ipv6_enabled else ""
        self._row(
            f"{site_upper} {desc} INFRA block ({type_label})",
            str(self.infra_net), infra_v6_str, "", "", "", "",
        )
        self._row(
            f"{site_upper} {desc} USER block ({type_label})",
            str(self.user_net), user_v6_str, "", "", "", "",
        )

        # Allocate infra subnets
        crosslink_prefix = int(self.subnet_sizes["crosslinks"].strip("/"))
        self.crosslink_base = ipaddress.IPv4Network(
            f"{self.infra_net.network_address}/{crosslink_prefix}", strict=False
        )

        # --- Crosslinks ---
        self._generate_crosslinks()

        # --- Loopbacks ---
        self._generate_loopbacks()

        # --- User VLANs (Data, Voice, Guest) ---
        self._generate_user_vlans()

        # --- VLAN 300 Services ---
        self._generate_vlan300()

        return self.rows

    def _generate_crosslinks(self):
        """Generate crosslink entries based on site type.

        IPv6 crosslinks use /127 subnets from the infra IPv6 prefix.
        The 4th hextet mirrors the IPv4 byte offset within the crosslink block.
        The crosslink summary line shows the infra prefix as a /56.
        """
        site = self.site
        services = self.cfg.get("services", {})
        lab = services.get("lab", False)
        voice = services.get("voice", False)

        # Crosslink summary: IPv6 /56 from infra prefix
        xl_v6_str = ""
        if self.ipv6_enabled:
            xl_v6_str = str(ipaddress.IPv6Network(
                f"{self.infra_v6.network_address}/{self.infra_v6.prefixlen + 4}", strict=False
            ))

        self._row(f"{site} Crosslinks", str(self.crosslink_base), xl_v6_str, "", "", "", "")

        if self.site_type == "small":
            self._crosslink_entry(
                f"{site}-wan-gw1 <-> {site}-console-server1",
                f"{site}-wan-gw1", "gig0-0-3",
                f"{site}-console-server1", "gig0-0-0",
            )
            for floor_info in self.cfg["floors"]:
                floor = floor_info["floor"]
                for stack in floor_info.get("switch_stacks", [{"stack_id": 1}]):
                    sw_name = floor_sw_name(site, floor, stack["stack_id"])
                    self._crosslink_entry(
                        f"{site}-wan-gw1 <-> {sw_name}",
                        f"{site}-wan-gw1", "ten0-0-4",
                        sw_name, "twe1-1-1",
                    )
            if lab:
                for floor_info in self.cfg["floors"]:
                    floor = floor_info["floor"]
                    for stack in floor_info.get("switch_stacks", [{"stack_id": 1}]):
                        sw_name = floor_sw_name(site, floor, stack["stack_id"])
                        self._crosslink_entry(
                            f"{sw_name} <-> {site}-lab-gw1",
                            sw_name, "twe1-1-3",
                            f"{site}-lab-gw1", "ten0-0-4",
                        )
        else:  # medium
            self._crosslink_entry(
                f"{site}-wan-gw1 <-> {site}-core-sw1",
                f"{site}-wan-gw1", "ten0-0-4",
                f"{site}-core-sw1", "hun1-0-49",
            )
            self._crosslink_entry(
                f"{site}-wan-gw1 <-> {site}-core-sw2",
                f"{site}-wan-gw1", "ten0-0-5",
                f"{site}-core-sw2", "hun1-0-49",
            )
            self._crosslink_entry(
                f"{site}-wan-gw2 <-> {site}-core-sw1",
                f"{site}-wan-gw2", "ten0-0-4",
                f"{site}-core-sw1", "hun1-0-50",
            )
            self._crosslink_entry(
                f"{site}-wan-gw2 <-> {site}-core-sw2",
                f"{site}-wan-gw2", "ten0-0-5",
                f"{site}-core-sw2", "hun1-0-50",
            )
            self._crosslink_entry(
                f"{site}-wan-gw1 <-> {site}-wan-gw2",
                f"{site}-wan-gw1", "gig0-0-0",
                f"{site}-wan-gw2", "gig0-0-0",
            )
            # WAN reserved (no DNS entries, just subnet)
            wan_offset1 = self.crosslink_offset
            wan_sub1 = self._next_crosslink()
            wan_v6_1 = self._infra_v6_wan_transit(wan_offset1)
            wan_v6_1_str = str(wan_v6_1) if wan_v6_1 else ""
            self._row(f"{site}-wan-gw1 <-> WAN", str(wan_sub1), wan_v6_1_str, "", "", "", "")

            wan_offset2 = self.crosslink_offset
            wan_sub2 = self._next_crosslink()
            wan_v6_2 = self._infra_v6_wan_transit(wan_offset2)
            wan_v6_2_str = str(wan_v6_2) if wan_v6_2 else ""
            self._row(f"{site}-wan-gw2 <-> WAN", str(wan_sub2), wan_v6_2_str, "", "", "", "")
            self._blank()

            self._crosslink_entry(
                f"{site}-core-sw1 <-> {site}-console-server1",
                f"{site}-core-sw1", "twe1-0-15",
                f"{site}-console-server1", "ten0-0-4",
            )
            self._crosslink_entry(
                f"{site}-core-sw2 <-> {site}-console-server1",
                f"{site}-core-sw2", "twe1-0-15",
                f"{site}-console-server1", "ten0-0-5",
            )

            if lab:
                self._crosslink_entry(
                    f"{site}-core-sw1 <-> {site}-lab-gw1",
                    f"{site}-core-sw1", "twe1-0-11",
                    f"{site}-lab-gw1", "ten0-0-0",
                )
                self._crosslink_entry(
                    f"{site}-core-sw1 <-> {site}-lab-gw2",
                    f"{site}-core-sw1", "twe1-0-12",
                    f"{site}-lab-gw2", "ten0-0-0",
                )
                self._crosslink_entry(
                    f"{site}-core-sw2 <-> {site}-lab-gw1",
                    f"{site}-core-sw2", "twe1-0-11",
                    f"{site}-lab-gw1", "ten0-0-1",
                )
                self._crosslink_entry(
                    f"{site}-core-sw2 <-> {site}-lab-gw2",
                    f"{site}-core-sw2", "twe1-0-12",
                    f"{site}-lab-gw2", "ten0-0-1",
                )
                self._crosslink_entry(
                    f"{site}-lab-gw1 <-> {site}-lab-gw2",
                    f"{site}-lab-gw1", "ten0-0-2",
                    f"{site}-lab-gw2", "ten0-0-2",
                )

            if voice:
                self._crosslink_entry(
                    f"{site}-core-sw1 <-> {site}-voice-gw1",
                    f"{site}-core-sw1", "twe1-0-13",
                    f"{site}-voice-gw1", "ten0-0-4",
                )
                self._crosslink_entry(
                    f"{site}-core-sw2 <-> {site}-voice-gw1",
                    f"{site}-core-sw2", "twe1-0-13",
                    f"{site}-voice-gw1", "ten0-0-5",
                )

    def _generate_loopbacks(self):
        """Generate loopback address entries (IPv4 + IPv6).

        IPv6 loopback addresses embed the IPv4 loopback into the host portion.
        E.g., 10.15.26.129 -> ::a0f:1a81
        """
        site = self.site
        services = self.cfg.get("services", {})
        lab = services.get("lab", False)
        voice = services.get("voice", False)

        lo_prefix = int(self.subnet_sizes["loopbacks"].strip("/"))
        if self.site_type == "small":
            lo_base_offset = 64
        else:
            lo_base_offset = 128

        lo_base = ipaddress.IPv4Address(int(self.infra_net.network_address) + lo_base_offset)
        lo_net = ipaddress.IPv4Network(f"{lo_base}/{lo_prefix}", strict=False)

        # IPv6 loopback /64 from infra prefix
        lo_v6_net = self._infra_v6_loopback_net()
        lo_v6_str = str(lo_v6_net) if lo_v6_net else ""
        lo_v6_base = int(lo_v6_net.network_address) if lo_v6_net else 0

        loopback_devices = []
        if self.site_type == "small":
            loopback_devices.append(f"{site}-wan-gw1")
            loopback_devices.append(f"{site}-console-server1")
            for floor_info in self.cfg["floors"]:
                floor = floor_info["floor"]
                for stack in floor_info.get("switch_stacks", [{"stack_id": 1}]):
                    loopback_devices.append(floor_sw_name(site, floor, stack["stack_id"]))
            if lab:
                loopback_devices.append(f"{site}-lab-gw1")
        else:
            loopback_devices.append(f"{site}-wan-gw1")
            loopback_devices.append(f"{site}-wan-gw2")
            loopback_devices.append(f"{site}-core-sw1")
            loopback_devices.append(f"{site}-core-sw2")
            loopback_devices.append(f"{site}-console-server1")
            for floor_info in self.cfg["floors"]:
                floor = floor_info["floor"]
                for stack in floor_info.get("switch_stacks", [{"stack_id": 1}]):
                    loopback_devices.append(floor_sw_name(site, floor, stack["stack_id"]))
            if lab:
                loopback_devices.append(f"{site}-lab-gw1")
                loopback_devices.append(f"{site}-lab-gw2")
            if voice:
                loopback_devices.append(f"{site}-voice-gw1")

        lo_hosts = list(lo_net.hosts())
        first = True
        for i, dev in enumerate(loopback_devices):
            ip = str(lo_hosts[i])
            v6_addr = ""
            if lo_v6_net:
                # Embed the IPv4 loopback address into the IPv6 host portion
                v6_addr = str(ipaddress.IPv6Address(lo_v6_base + self._ipv4_to_v6_host(ip)))
            if first:
                self._row(f"{site} Loopbacks", str(lo_net), lo_v6_str, "", dev, ip, v6_addr)
                first = False
            else:
                self._row("", "", "", "", dev, ip, v6_addr)
        self._blank()

    def _generate_vlan300(self):
        """Generate VLAN 300 (Services) entries with APs (IPv4 + IPv6).

        Services VLAN uses the infra IPv6 prefix at a fixed hextet offset.
        Gateway IPs use ::1, ::2, ::3. APs get N/A for IPv6.
        """
        site = self.site

        v300_prefix = int(self.subnet_sizes["vlan_300"].strip("/"))
        if self.site_type == "small":
            v300_offset = 128
            v300_base = ipaddress.IPv4Address(int(self.infra_net.network_address) + v300_offset)
        else:
            v300_base = ipaddress.IPv4Address(int(self.infra_net.network_address) + 256)

        v300_net = ipaddress.IPv4Network(f"{v300_base}/{v300_prefix}", strict=False)
        v300_hosts = list(v300_net.hosts())

        # IPv6 for VLAN 300 Services from infra prefix
        v6_net = self._infra_v6_services_net()
        v6_str = str(v6_net) if v6_net else ""
        v6_base = int(v6_net.network_address) if v6_net else 0

        if self.site_type == "small":
            for floor_info in self.cfg["floors"]:
                floor = floor_info["floor"]
                for stack in floor_info.get("switch_stacks", [{"stack_id": 1}]):
                    sw = floor_sw_name(site, floor, stack["stack_id"])
                    v6_gw = str(ipaddress.IPv6Address(v6_base + 1)) if v6_net else ""
                    self._row(
                        f"{site} VLAN 300 Services",
                        str(v300_net), v6_str, "",
                        f"{sw}-vlan300", str(v300_hosts[0]), v6_gw,
                    )
        else:
            v6_vip = str(ipaddress.IPv6Address(v6_base + 1)) if v6_net else ""
            v6_sw1 = str(ipaddress.IPv6Address(v6_base + 2)) if v6_net else ""
            v6_sw2 = str(ipaddress.IPv6Address(v6_base + 3)) if v6_net else ""
            self._row(
                f"{site} VLAN 300 Services",
                str(v300_net), v6_str, "",
                f"hsrp-{str(v300_hosts[0]).replace('.', '-')}",
                str(v300_hosts[0]), v6_vip,
            )
            self._row("", "", "", "", f"{site}-core-sw1-vlan300", str(v300_hosts[1]), v6_sw1)
            self._row("", "", "", "", f"{site}-core-sw2-vlan300", str(v300_hosts[2]), v6_sw2)

        # AP entries on VLAN 300 — APs get N/A for IPv6
        ap_ip_idx = 10
        for floor_info in self.cfg["floors"]:
            floor = floor_info["floor"]
            ap_count = floor_info.get("ap_count", 0)
            for ap_num in range(1, ap_count + 1):
                ap = ap_name(site, floor, ap_num)
                if ap_ip_idx < len(v300_hosts):
                    self._row("", "", "", "", ap, str(v300_hosts[ap_ip_idx]), "N/A")
                ap_ip_idx += 1

        self._blank()

    def _generate_user_vlans(self):
        """Generate user VLAN entries (100 Data, 150 Voice, 200 Guest).

        Subnets are allocated from the user block in descending size order
        to maintain proper alignment, then output in the requested order.
        """
        site = self.site
        services = self.cfg.get("services", {})
        voice = services.get("voice", False)

        user_base = int(self.user_net.network_address)

        v100_prefix = int(self.subnet_sizes["vlan_100"].strip("/"))
        v200_prefix = int(self.subnet_sizes["vlan_200"].strip("/"))
        v150_prefix = int(self.subnet_sizes["vlan_150"].strip("/"))

        allocs = sorted([
            ("100", v100_prefix),
            ("200", v200_prefix),
            ("150", v150_prefix),
        ], key=lambda x: x[1])

        nets = {}
        offset = 0
        for vlan_id, prefix in allocs:
            net = ipaddress.IPv4Network(
                f"{ipaddress.IPv4Address(user_base + offset)}/{prefix}", strict=False
            )
            nets[vlan_id] = net
            offset += 2 ** (32 - prefix)

        v100_net = nets["100"]
        v150_net = nets["150"]
        v200_net = nets["200"]

        # IPv6 subnets for user VLANs from user IPv6 prefix
        v100_v6 = self._user_v6_vlan_net("100") if self.ipv6_enabled else None
        v150_v6 = self._user_v6_vlan_net("150") if self.ipv6_enabled else None
        v200_v6 = self._user_v6_vlan_net("200") if self.ipv6_enabled else None

        # --- VLAN 100 Data ---
        self._emit_user_vlan(
            site, "100", "Data", v100_net, v100_v6, include_floor_sw=True
        )

        # --- VLAN 150 Voice ---
        self._emit_user_vlan(
            site, "150", "Voice", v150_net, v150_v6, include_floor_sw=False
        )

        # --- VLAN 200 Guest ---
        self._emit_user_vlan(
            site, "200", "Guest", v200_net, v200_v6, include_floor_sw=False
        )

    def _emit_user_vlan(self, site, vlan_id, vlan_name, v4_net, v6_net, include_floor_sw=False):
        """Emit rows for a single user VLAN with IPv4 + IPv6.

        Gateway IPs use simple sequential ::1, ::2, ::3.
        Floor switch management IPs embed their IPv4 address into the IPv6 host portion.
        """
        v4_hosts = list(v4_net.hosts())
        v6_str = str(v6_net) if v6_net else ""
        v6_base = int(v6_net.network_address) if v6_net else 0

        if self.site_type == "small":
            for floor_info in self.cfg["floors"]:
                floor = floor_info["floor"]
                for stack in floor_info.get("switch_stacks", [{"stack_id": 1}]):
                    sw = floor_sw_name(site, floor, stack["stack_id"])
                    v6_gw = str(ipaddress.IPv6Address(v6_base + 1)) if v6_net else ""
                    self._row(
                        f"{site} VLAN {vlan_id} {vlan_name}",
                        str(v4_net), v6_str, "",
                        f"{sw}-vlan{vlan_id}", str(v4_hosts[0]), v6_gw,
                    )
        else:
            v6_vip = str(ipaddress.IPv6Address(v6_base + 1)) if v6_net else ""
            v6_sw1 = str(ipaddress.IPv6Address(v6_base + 2)) if v6_net else ""
            v6_sw2 = str(ipaddress.IPv6Address(v6_base + 3)) if v6_net else ""
            self._row(
                f"{site} VLAN {vlan_id} {vlan_name}",
                str(v4_net), v6_str, "",
                f"hsrp-{str(v4_hosts[0]).replace('.', '-')}",
                str(v4_hosts[0]), v6_vip,
            )
            self._row("", "", "", "", f"{site}-core-sw1-vlan{vlan_id}", str(v4_hosts[1]), v6_sw1)
            self._row("", "", "", "", f"{site}-core-sw2-vlan{vlan_id}", str(v4_hosts[2]), v6_sw2)

            if include_floor_sw:
                sw_ip_idx = 3
                for floor_info in self.cfg["floors"]:
                    floor = floor_info["floor"]
                    for stack in floor_info.get("switch_stacks", [{"stack_id": 1}]):
                        sw = floor_sw_name(site, floor, stack["stack_id"])
                        ipv4_str = str(v4_hosts[sw_ip_idx])
                        # Embed IPv4 into IPv6 host portion
                        v6_sw = str(ipaddress.IPv6Address(
                            v6_base + self._ipv4_to_v6_host(ipv4_str)
                        )) if v6_net else ""
                        self._row("", "", "", "", sw, ipv4_str, v6_sw)
                        sw_ip_idx += 1
        self._blank()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 generate_address_plan.py <input_yaml>")
        print("Example: python3 generate_address_plan.py inputs/nyc_input.yaml")
        sys.exit(1)

    input_path = sys.argv[1]
    if not os.path.isabs(input_path):
        input_path = os.path.join(SCRIPT_DIR, input_path)

    input_cfg = load_yaml(input_path)
    site = input_cfg["site"]

    plan = AddressPlan(input_cfg)
    rows = plan.generate()

    output_dir = os.path.join(SCRIPT_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{site}-address-plan.csv")

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(f"Address plan generated: {output_path}")


if __name__ == "__main__":
    main()
