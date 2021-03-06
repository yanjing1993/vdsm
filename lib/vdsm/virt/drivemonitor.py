#
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
from __future__ import division

import libvirt

from vdsm.config import config
from vdsm.virt.vmdevices import lookup
from vdsm.virt.vmdevices import storage


class ImprobableResizeRequestError(RuntimeError):
    pass


class DriveMonitor(object):
    """
    Track the highest allocation of thin-provisioned drives
    of a Vm, triggering the extension flow when needed.
    """

    def __init__(self, vm, log, enabled=True):
        self._vm = vm
        self._log = log
        self._enabled = enabled
        self._events_enabled = config.getboolean(
            'irs', 'enable_block_threshold_event')

    def events_enabled(self):
        return self._events_enabled

    def enabled(self):
        return self._enabled

    def enable(self):
        self._enabled = True
        self._log.info('Enabling drive monitoring')

    def disable(self):
        self._enabled = False
        self._log.info('Disabling drive monitoring')

    def monitoring_needed(self):
        """
        Return True if a vm needs drive monitoring in this cycle.

        This is called every 2 seconds (configurable) by the periodic system.
        If this returns True, the periodic system will invoke
        monitor_drives during this periodic cycle.
        """
        return self._enabled and bool(self.monitored_drives())

    def set_threshold(self, drive, apparentsize):
        """
        Set the libvirt block threshold on the given drive, enabling
        libvirt to deliver the event when the threshold is crossed.
        Does nothing if the `_events_enabled` attribute is Falsey.

        Call this method when you need to set one initial block threshold
        (e.g. first time Vdsm monitors one drive), or after one volume
        extension, or when the top layer changes (after snapshot, after
        live storage migration completes).

        Args:
            drive: A storage.Drive object
            apparentsize: The drive apparent size in bytes (int)
        """
        if not self._events_enabled:
            return
        # watermarkLimit tells us the minimum amount of free space a thin
        # provisioned must have to avoid the extension.
        # If the free space falls below this limit, we should extend.
        # thus the following holds:
        # Extend if
        #    physical - allocation < limit
        # or if (equivalent to the above)
        #    allocation > physical - limit
        # the libvirt event fires if allocation >= threshold,
        # so we just compute
        #    threshold = physical - limit

        # 1  is the minimum meaningful threshold.
        # 0  is valid, but should be used only in clear_threshold
        # <0 means that apparentsize is too low, likely storage issue
        # that should be already handled -or at least notified- elsewhere.
        threshold = max(1, apparentsize - drive.watermarkLimit)

        self._log.info(
            'setting block threshold to %d bytes for drive %r '
            '(apparentsize %d)',
            threshold, drive.name, apparentsize
        )
        try:
            # TODO: find a good way to expose Vm._dom as public property.
            # we are running out of names in Vm class.
            self._vm._dom.setBlockThreshold(drive.name, threshold)
        except libvirt.libvirtError as exc:
            # The drive threshold_state can be UNSET or EXCEEDED, and
            # this ensures that we will attempt to set the threshold later.
            drive.threshold_state = storage.BLOCK_THRESHOLD.UNSET
            self._log.error(
                'Failed to set block threshold on %r (%s): %s',
                drive.name, drive.path, exc)
        else:
            drive.threshold_state = storage.BLOCK_THRESHOLD.SET

    def clear_threshold(self, drive, index=None):
        """
        Clear the libvirt block threshold on the given drive, disabling
        libvirt events.

        Args:
            drive: A storage.Drive object
            index: Optional index (int) of the element of the backing chain
                   to clear. If None (default), use the top layer.
        """
        if not self._events_enabled:
            return

        if index is None:
            target = drive.name
        else:
            target = '%s[%d]' % (drive.name, index)
        self._log.info('clearing block threshold for drive %r', target)
        # undocumented at libvirt level, need to deep dive to QEMU level
        # to learn this: set threshold to 0 disable the notification
        # another alternative could be just clear_threshold the events
        # we receive with monitoring disabled (flag at either Vm/drive
        # level). We will have races anyway.
        # TODO: file a libvirt documentation bug
        self._vm._dom.setBlockThreshold(target, 0)

    def on_block_threshold(self, dev, path, threshold, excess):
        """
        Callback to be executed in the libvirt event handler when
        a BLOCK_THRESHOLD event is delivered.

        Args:
            dev: device name (e.g. vda, sdb)
            path: device path
            threshold: the threshold (in bytes) that was exceeded
                       causing the event to trigger
            excess: amount (in bytes) written past the threshold
        """
        self._log.info('block threshold %d exceeded on %r (%s)',
                       threshold, dev, path)
        try:
            drive = lookup.drive_by_name(self._vm.getDiskDevices()[:], dev)
        except LookupError:
            self._log.warning(
                'Unknown drive %r for vm %s - ignored block threshold event',
                dev, self._vm.id)
        else:
            drive.on_block_threshold(path)

    def monitored_drives(self):
        """
        Return the drives that need to be checked for extension
        on the next monitoring cycle.

        Returns:
            iterable of storage.Drives that needs to be checked
            for extension.
        """
        return [drive for drive in self._vm.getDiskDevices()
                if drive.needs_monitoring(self._events_enabled)]

    def should_extend_volume(self, drive, volumeID, capacity, alloc, physical):
        nextPhysSize = drive.getNextVolumeSize(physical, capacity)

        # NOTE: the intent of this check is to prevent faulty images to
        # trick qemu in requesting extremely large extensions (BZ#998443).
        # Probably the definitive check would be comparing the allocated
        # space with capacity + format_overhead. Anyway given that:
        #
        # - format_overhead is tricky to be computed (it depends on few
        #   assumptions that may change in the future e.g. cluster size)
        # - currently we allow only to extend by one chunk at time
        #
        # the current check compares alloc with the next volume size.
        # It should be noted that alloc cannot be directly compared with
        # the volume physical size as it includes also the clusters not
        # written yet (pending).
        if alloc > nextPhysSize:
            msg = ("Improbable extension request for volume %s on domain "
                   "%s, pausing the VM to avoid corruptions (capacity: %s, "
                   "allocated: %s, physical: %s, next physical size: %s)" %
                   (volumeID, drive.domainID, capacity, alloc, physical,
                    nextPhysSize))
            self._log.error(msg)
            self._vm.pause(pauseCode='EOTHER')
            raise ImprobableResizeRequestError(msg)

        if physical >= drive.getMaxVolumeSize(capacity):
            # The volume was extended to the maximum size. physical may be
            # larger than maximum volume size since it is rounded up to the
            # next lvm extent.
            return False

        if physical - alloc < drive.watermarkLimit:
            return True
        return False

    def update_threshold_state_exceeded(self, drive):
        if (drive.threshold_state != storage.BLOCK_THRESHOLD.EXCEEDED and
                self.events_enabled()):
            # if the threshold is wrongly set below the current allocation,
            # for example because of delays in handling the event,
            # or if the VM writes too fast, we will never receive an event.
            # We need to set the drive threshold to EXCEEDED both if we receive
            # one event or if we found that the threshold was exceeded during
            # the _shouldExtendVolume check.
            drive.threshold_state = storage.BLOCK_THRESHOLD.EXCEEDED
            self._log.info(
                "Drive %s needs to be extended, forced threshold_state "
                "to exceeded", drive.name)
