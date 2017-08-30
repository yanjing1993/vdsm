# Copyright 2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

import abc

import six

from vdsm import commands
from vdsm.network.lldp import EnableLldpError
from vdsm.network.lldp import DisableLldpError
from vdsm.network.lldp import TlvReportLldpError


SYSTEMCTL = '/usr/bin/systemctl'
LLDPTOOL = '/usr/sbin/lldptool'
LLDPAD_SERVICE = 'lldpad.service'


def is_lldpad_service_running():
    rc, _, _ = commands.execCmd([SYSTEMCTL, 'status', LLDPAD_SERVICE])
    return rc == 0


def is_lldptool_functional():
    rc, _, _ = commands.execCmd([LLDPTOOL, '-ping'])
    return rc == 0


def enable_lldp_on_iface(iface, rx_only=True):
    rc, out, err = commands.execCmd(
        [LLDPTOOL, 'set-lldp', '-i', iface,
         'adminStatus=' + ('rx' if rx_only else 'rxtx')], raw=True)
    if rc:
        raise EnableLldpError(rc, out, err, iface)


def disable_lldp_on_iface(iface):
    rc, out, err = commands.execCmd([LLDPTOOL, 'set-lldp', '-i', iface,
                                     'adminStatus=disabled'], raw=True)
    if rc:
        raise DisableLldpError(rc, out, err, iface)


def is_lldp_enabled_on_iface(iface):
    rc, out, err = commands.execCmd([LLDPTOOL, 'get-lldp', '-i', iface,
                                     'adminStatus'], raw=True)
    if rc:
        return False
    keyval = out.strip().split('=', 1)
    if len(keyval) == 2 and keyval[0] == 'adminStatus':
        return not keyval[1] == 'disabled'
    return False


def get_tlvs(iface):
    """
    Report the specified tlv identifiers.

    :param iface: The interface to query.
    :return: TLV reports in a dict format where the TLV ID/s are the keys.
    """
    rc, stdout, err = commands.execCmd([LLDPTOOL, 'get-tlv', '-n', '-i',
                                        iface], raw=True)
    if rc == 0:
        return _parse_tlvs(stdout)
    else:
        raise TlvReportLldpError(rc, stdout, err, iface)


def _parse_tlvs(text):
    tlvs_report = []
    for description, properties in _separate_tlvs(text):
        if description in TLVS_BY_DESCRIPTION:
            tlv = TLVS_BY_DESCRIPTION[description]
            tlv_info = {'type': tlv.type,
                        'name': tlv.name,
                        'properties': tlv.parse_properties(properties)}
            if tlv.oui:
                tlv_info['oui'] = tlv.oui
                tlv_info['subtype'] = tlv.subtype

            tlvs_report.append(tlv_info)

    return tlvs_report


def _separate_tlvs(text):
    lines = text.splitlines()
    for tlv_records in _next_tlv(lines):
        yield tlv_records[0], tlv_records[1:]


def _next_tlv(lines):
    tlv_end_idx = 0
    tlv_start_idx = 0
    while tlv_start_idx < len(lines):
        tlv_end_idx += 1
        if lines[tlv_start_idx]:
            while _is_property_line(lines, tlv_end_idx):
                tlv_end_idx += 1
            yield lines[tlv_start_idx:tlv_end_idx]
        tlv_start_idx = tlv_end_idx


def _is_property_line(lines, line_number):
    return line_number < len(lines) and lines[line_number].startswith('\t')


class OUI(object):
    """ Organizationally Unique Identifier """
    IEEE8021 = 0x0080c2


class Tlv(object):
    def __init__(self, tlv_type, oui, subtype, name, description,
                 property_parser):
        self.type = tlv_type
        self.oui = oui
        self.subtype = subtype
        self.name = name
        self.description = description
        self._property_parser = property_parser

    def parse_properties(self, properties_text):
        return self._property_parser.parse(self.name, properties_text)


@six.add_metaclass(abc.ABCMeta)
class PropertyParser(object):
    @abc.abstractmethod
    def parse(self, tlv_name, property_lines):
        pass

    def _parse_subtype_value(self, property_line, tlv_name):
        tokens = property_line.split(':', 1)
        return {'%s subtype' % tlv_name: tokens[0].strip(),
                tlv_name: tokens[1].strip()}


class ChassisIdParser(PropertyParser):
    def parse(self, tlv_name, property_lines):
        return self._parse_subtype_value(property_lines[0], 'chassis ID')


class PortIdParser(PropertyParser):
    def parse(self, tlv_name, property_lines):
        return self._parse_subtype_value(property_lines[0], 'port ID')


class SingleStringPropertyParser(PropertyParser):
    def parse(self, tlv_name, property_lines):
        return {tlv_name.lower(): property_lines[0].strip()}


class MultiStringPropertyParser(PropertyParser):
    def parse(self, tlv_name, property_lines):
        return {k.lower(): v for k, v in
                self._split_property_lines(property_lines).items()}

    def _split_property_lines(self, lines):
        properties = {}
        for line in lines:
            tokens = line.split(':', 1)
            properties[tokens[0].strip()] = tokens[-1].strip()
        return properties


class ManagmentAddressParser(PropertyParser):
    def parse(self, tlv_name, property_lines):
        properties = self._parse_subtype_value(property_lines[0],
                                               'management address')
        properties.update(self._parse_subtype_value(
            property_lines[1], 'interface numbering'))
        properties['object identifier'] = property_lines[2].split(
            ':', 1)[-1].strip()
        return properties


class PortVlanIdParser(PropertyParser):
    def parse(self, tlv_name, property_lines):
        return {'Port VLAN ID': property_lines[0].split(':', 1)[-1].strip()}


class VlanNameParser(PropertyParser):
    def parse(self, tlv_name, property_lines):
        tokens = property_lines[0].split(':', 1)
        return {'VLAN Name': tokens[0].split(' ', 1)[-1].strip(),
                'VLAN ID': tokens[1].strip()}


TLVS = frozenset([
    Tlv(1, 0, 0, 'Chassis ID', 'Chassis ID TLV', ChassisIdParser()),
    Tlv(2, 0, 0, 'Port ID', 'Port ID TLV', PortIdParser()),
    Tlv(3, 0, 0, 'Time to Live', 'Time to Live TLV',
        SingleStringPropertyParser()),
    Tlv(4, 0, 0, 'Port Description', 'Port Description TLV',
        SingleStringPropertyParser()),
    Tlv(5, 0, 0, 'System Name', 'System Name TLV',
        SingleStringPropertyParser()),
    Tlv(6, 0, 0, 'System Description', 'System Description TLV',
        SingleStringPropertyParser()),
    Tlv(7, 0, 0, 'System Capabilities', 'System Capabilities TLV',
        MultiStringPropertyParser()),
    Tlv(8, 0, 0, 'Management Address', 'Management Address TLV',
        ManagmentAddressParser()),
    Tlv(0x7f, OUI.IEEE8021, 1, 'Port VLAN ID', 'Port VLAN ID TLV',
        PortVlanIdParser()),
    Tlv(0x7f, OUI.IEEE8021, 3, 'VLAN Name', 'VLAN Name TLV',
        VlanNameParser())
])

TLVS_BY_DESCRIPTION = {tlv.description: tlv for tlv in TLVS}
