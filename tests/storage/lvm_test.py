#
# Copyright 2012-2019 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division

import os
import time
import uuid

import pytest

from vdsm.common import commands
from vdsm.common import concurrent
from vdsm.common import constants

from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import lvm
from vdsm.storage import misc

from . marks import requires_root, xfail_python3


# TODO: replace the filter tests with cmd tests.


def test_build_filter():
    devices = ("/dev/mapper/a", "/dev/mapper/b")
    expected = '["a|^/dev/mapper/a$|^/dev/mapper/b$|", "r|.*|"]'
    assert expected == lvm._buildFilter(devices)


def test_build_filter_quoting():
    devices = (r"\x20\x24\x7c\x22\x28",)
    expected = r'["a|^\\x20\\x24\\x7c\\x22\\x28$|", "r|.*|"]'
    assert expected == lvm._buildFilter(devices)


def test_build_filter_no_devices():
    # This special case is possible on a system without any multipath device.
    # LVM commands will succeed, returning no info.
    expected = '["r|.*|"]'
    assert expected == lvm._buildFilter(())


def test_build_filter_with_user_devices(monkeypatch):
    monkeypatch.setattr(lvm, "USER_DEV_LIST", ("/dev/b",))
    expected_filter = '["a|^/dev/a$|^/dev/b$|^/dev/c$|", "r|.*|"]'
    assert expected_filter == lvm._buildFilter(("/dev/a", "/dev/c"))


# TODO: replace the build command tests with cmd tests.


def test_build_config():
    expected = (
        'devices { '
        ' preferred_names=["^/dev/mapper/"] '
        ' ignore_suspended_devices=1 '
        ' write_cache_state=0 '
        ' disable_after_error_count=3 '
        ' filter=["a|^/dev/a$|^/dev/b$|", "r|.*|"] '
        '} '
        'global { '
        ' locking_type=1 '
        ' prioritise_write_locks=1 '
        ' wait_for_locks=1 '
        ' use_lvmetad=0 '
        '} '
        'backup { '
        ' retain_min=50 '
        ' retain_days=0 '
        '}'
    )
    assert expected == lvm._buildConfig(
        dev_filter='["a|^/dev/a$|^/dev/b$|", "r|.*|"]',
        locking_type="1")


@pytest.fixture
def fake_devices(monkeypatch):
    devices = ["/dev/mapper/a", "/dev/mapper/b"]

    # Initial devices for LVMCache tests.
    monkeypatch.setattr(
        lvm.multipath,
        "getMPDevNamesIter",
        lambda: tuple(devices))

    return devices


def test_build_command_long_filter(fake_devices):
    # If the devices are not specified, include all devices reported by
    # multipath.
    lc = lvm.LVMCache()
    cmd = lc._addExtraCfg(["lvs", "-o", "+tags"])

    assert cmd == [
        constants.EXT_LVM,
        "lvs",
        "--config",
        lvm._buildConfig(
            dev_filter=lvm._buildFilter(fake_devices),
            locking_type="1"),
        "-o", "+tags",
    ]


def test_rebuild_filter_after_invaliation(fake_devices):
    # Check that adding a device and invalidating the filter rebuilds the
    # config with the correct filter.
    lc = lvm.LVMCache()
    lc._addExtraCfg(["lvs"])

    fake_devices.append("/dev/mapper/c")
    lc.invalidateFilter()

    cmd = lc._addExtraCfg(["lvs"])
    assert cmd[3] == lvm._buildConfig(
        dev_filter=lvm._buildFilter(fake_devices),
        locking_type="1")


def test_build_command_read_only(fake_devices):
    # When cache in read-write mode, use locking_type=1
    lc = lvm.LVMCache()
    cmd = lc._addExtraCfg(["lvs", "-o", "+tags"])
    assert " locking_type=1 " in cmd[3]

    # When cache in read-only mode, use locking_type=4
    lc.set_read_only(True)
    cmd = lc._addExtraCfg(["lvs", "-o", "+tags"])
    assert " locking_type=4 " in cmd[3]


class FakeRunner(object):
    """
    Simulate a command failing multiple times before suceeding. This is the
    case when running lvm read-only command with a very busy SPM.

    By default, the first call will succeed, returning the given rc, out, and
    err.

    If retries is set, requires retires extra failing calls to succeed.

    To validate the call, inspect the calls instance variable.
    """

    def __init__(self, rc=0, out=b"", err=b"", retries=0, delay=0.0):
        self.rc = rc
        self.out = out
        self.err = err
        self.retries = retries
        self.delay = delay
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))

        if self.delay:
            time.sleep(self.delay)

        if self.retries > 0:
            self.retries -= 1
            return 1, b"", b"fake error"

        return self.rc, self.out, self.err


@pytest.fixture
def fake_runner(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(misc, "execCmd", runner)
    # Disable delay to speed up testing.
    monkeypatch.setattr(lvm.LVMCache, "RETRY_DELAY", 0)
    return runner


def test_cmd_success(fake_devices, fake_runner):
    lc = lvm.LVMCache()
    rc, out, err = lc.cmd(["lvs", "-o", "+tags"])

    assert rc == 0
    assert len(fake_runner.calls) == 1

    cmd, kwargs = fake_runner.calls[0]
    assert cmd == [
        constants.EXT_LVM,
        "lvs",
        "--config",
        lvm._buildConfig(
            dev_filter=lvm._buildFilter(fake_devices),
            locking_type="1"),
        "-o", "+tags",
    ]

    assert kwargs == {"sudo": True}


def test_cmd_error(fake_devices, fake_runner):
    lc = lvm.LVMCache()

    # Require 2 calls to succeed.
    assert lc.READ_ONLY_RETRIES > 1
    fake_runner.retries = 1

    # Since the filter is correct, the error should be propagated to the caller
    # after the first call.
    rc, out, err = lc.cmd(["lvs", "-o", "+tags"])

    assert rc == 1
    assert len(fake_runner.calls) == 1


def test_cmd_retry_filter_stale(fake_devices, fake_runner):
    # Make a call to load the cache.
    initial_devices = fake_devices[:]
    lc = lvm.LVMCache()
    lc.cmd(["fake"])
    del fake_runner.calls[:]

    # Add a new device to the system. This will makes the cached filter stale,
    # so the command will be retried with a new filter.
    fake_devices.append("/dev/mapper/c")

    # Require 2 calls to succeed.
    assert lc.READ_ONLY_RETRIES > 1
    fake_runner.retries = 1

    rc, out, err = lc.cmd(["fake"])

    assert rc == 0
    assert len(fake_runner.calls) == 2

    # The first call used the stale cache filter.
    cmd, kwargs = fake_runner.calls[0]
    assert cmd == [
        constants.EXT_LVM,
        "fake",
        "--config",
        lvm._buildConfig(
            dev_filter=lvm._buildFilter(initial_devices),
            locking_type="1"),
    ]
    assert kwargs == {"sudo": True}

    # The seocnd call used a wider filter.
    cmd, kwargs = fake_runner.calls[1]
    assert cmd == [
        constants.EXT_LVM,
        "fake",
        "--config",
        lvm._buildConfig(
            dev_filter=lvm._buildFilter(fake_devices),
            locking_type="1"),
    ]
    assert kwargs == {"sudo": True}


def test_cmd_read_only(fake_devices, fake_runner):
    lc = lvm.LVMCache()
    lc.set_read_only(True)

    # Require 3 calls to succeed.
    assert lc.READ_ONLY_RETRIES > 2
    fake_runner.retries = 2

    rc, out, err = lc.cmd(["fake"])

    # Call should succeed after 3 identical calls.
    assert rc == 0
    assert len(fake_runner.calls) == 3
    assert len(set(repr(c) for c in fake_runner.calls)) == 1


def test_cmd_read_only_max_retries(fake_devices, fake_runner):
    lc = lvm.LVMCache()
    lc.set_read_only(True)

    # Require max retries to succeed.
    fake_runner.retries = lc.READ_ONLY_RETRIES
    rc, out, err = lc.cmd(["fake"])

    # Call should succeed (1 call + max retries).
    assert rc == 0
    assert len(fake_runner.calls) == lc.READ_ONLY_RETRIES + 1
    assert len(set(repr(c) for c in fake_runner.calls)) == 1


def test_cmd_read_only_max_retries_fail(fake_devices, fake_runner):
    lc = lvm.LVMCache()
    lc.set_read_only(True)

    # Require max retries + 1 to succeed.
    fake_runner.retries = lc.READ_ONLY_RETRIES + 1

    rc, out, err = lc.cmd(["fake"])

    # Call should fail (1 call + max retries).
    assert rc == 1
    assert len(fake_runner.calls) == lc.READ_ONLY_RETRIES + 1


def test_cmd_read_only_filter_stale(fake_devices, fake_runner):
    # Make a call to load the cache.
    initial_devices = fake_devices[:]
    lc = lvm.LVMCache()
    lc.cmd(["fake"])
    del fake_runner.calls[:]

    # Add a new device to the system. This will makes the cached filter stale,
    # so the command will be retried with a new filter.
    fake_devices.append("/dev/mapper/c")

    # Require max retries + 1 calls to succeed.
    fake_runner.retries = lc.READ_ONLY_RETRIES + 1

    lc.set_read_only(True)
    rc, out, err = lc.cmd(["fake"])

    # Call should succeed after one call with stale filter, one call with wider
    # filter and max retries identical calls.
    assert rc == 0
    assert len(fake_runner.calls) == lc.READ_ONLY_RETRIES + 2

    # The first call used the stale cache filter.
    cmd, kwargs = fake_runner.calls[0]
    assert cmd == [
        constants.EXT_LVM,
        "fake",
        "--config",
        lvm._buildConfig(
            dev_filter=lvm._buildFilter(initial_devices),
            locking_type="4"),
    ]
    assert kwargs == {"sudo": True}

    # The seocnd call used a wider filter.
    cmd, kwargs = fake_runner.calls[1]
    assert cmd == [
        constants.EXT_LVM,
        "fake",
        "--config",
        lvm._buildConfig(
            dev_filter=lvm._buildFilter(fake_devices),
            locking_type="4"),
    ]
    assert kwargs == {"sudo": True}

    # And then indentical retries with the wider filter.
    assert len(set(repr(c) for c in fake_runner.calls[1:])) == 1


def test_cmd_read_only_filter_stale_fail(fake_devices, fake_runner):
    # Make a call to load the cache.
    lc = lvm.LVMCache()
    lc.cmd(["fake"])
    del fake_runner.calls[:]

    # Add a new device to the system. This will makes the cached filter stale,
    # so the command will be retried with a new filter.
    fake_devices.append("/dev/mapper/c")

    # Require max retries + 2 calls to succeed.
    fake_runner.retries = lc.READ_ONLY_RETRIES + 2

    lc.set_read_only(True)
    rc, out, err = lc.cmd(["fake"])

    # Call should fail after max retries + 2 calls.
    assert rc == 1
    assert len(fake_runner.calls) == lc.READ_ONLY_RETRIES + 2


class Workers(object):

    def __init__(self):
        self.threads = []

    def start_thread(self, func, *args):
        t = concurrent.thread(func, args=args)
        t.start()
        self.threads.append(t)

    def join(self):
        for t in self.threads:
            t.join()


@pytest.fixture
def workers():
    workers = Workers()
    try:
        yield workers
    finally:
        workers.join()


@pytest.mark.parametrize("read_only", [True, False])
def test_command_concurrency(fake_devices, fake_runner, workers, read_only):
    # Test concurrent commands to reveal locking issues.
    lc = lvm.LVMCache()
    lc.set_read_only(read_only)

    fake_runner.delay = 0.2
    count = 50
    start = time.time()
    try:
        for i in range(count):
            workers.start_thread(lc.cmd, ["fake", i])
    finally:
        workers.join()

    elapsed = time.time() - start
    assert len(fake_runner.calls) == count

    # This takes about 1 second on my idle laptop. Add more time to avoid
    # failures on overloaded slave.
    assert elapsed < fake_runner.delay * count / lc.MAX_COMMANDS + 1.0


def test_change_read_only_mode(fake_devices, fake_runner, workers):
    # Test that changing read only wait for running commands, and new commands
    # wait for the read only change.
    lc = lvm.LVMCache()

    def run_after(delay, func, *args):
        time.sleep(delay)
        func(*args)

    fake_runner.delay = 0.3
    start = time.time()
    try:
        # Start few commands in read-write mode.
        for i in range(2):
            workers.start_thread(run_after, 0.0, lc.cmd, ["read-write"])

        # After 0.1 seconds change read only mode to True. Should wait for the
        # running commands before changing the mode.
        workers.start_thread(run_after, 0.1, lc.set_read_only, True)

        # After 0.2 seconds, start new commands. Should wait until the mode is
        # changed and run in read-only mode.
        for i in range(2):
            workers.start_thread(run_after, 0.2, lc.cmd, ["read-only"])
    finally:
        workers.join()

    elapsed = time.time() - start

    assert len(fake_runner.calls) == 4

    # The first 2 commands should run in read-write mode.
    for cmd, kwargs in fake_runner.calls[:2]:
        assert " locking_type=1 " in cmd[3]

    # The last 2 command should run in not read-only mode.
    for cmd, kwargs in fake_runner.calls[2:]:
        assert " locking_type=4 " in cmd[3]

    # The last 2 command can start only after the first 2 command finished.
    assert elapsed > fake_runner.delay * 2


@requires_root
@xfail_python3
@pytest.mark.root
def test_vg_create_remove_single_device(tmp_storage):
    dev_size = 20 * 1024**3
    dev = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev], "initial-tag", 128)

    vg = lvm.getVG(vg_name)
    assert vg.name == vg_name
    assert vg.pv_name == (dev,)
    assert vg.tags == ("initial-tag",)
    assert int(vg.extent_size) == 128 * 1024**2

    pv = lvm.getPV(dev)
    assert pv.name == dev
    assert pv.vg_name == vg_name
    assert int(pv.dev_size) == dev_size
    assert int(pv.mda_count) == 2
    assert int(pv.mda_used_count) == 2

    lvm.removeVG(vg_name)

    # We remove the VG
    with pytest.raises(se.VolumeGroupDoesNotExist):
        lvm.getVG(vg_name)

    # But keep the PVs, not sure why.
    pv = lvm.getPV(dev)
    assert pv.name == dev
    assert pv.vg_name == ""


@requires_root
@xfail_python3
@pytest.mark.root
def test_vg_create_multiple_devices(tmp_storage):
    dev_size = 10 * 1024**3
    dev1 = tmp_storage.create_device(dev_size)
    dev2 = tmp_storage.create_device(dev_size)
    dev3 = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev1, dev2, dev3], "initial-tag", 128)

    vg = lvm.getVG(vg_name)
    assert vg.name == vg_name
    assert sorted(vg.pv_name) == sorted((dev1, dev2, dev3))

    # The first pv (metadata pv) will have the 2 used metadata areas.
    pv = lvm.getPV(dev1)
    assert pv.name == dev1
    assert pv.vg_name == vg_name
    assert int(pv.dev_size) == dev_size
    assert int(pv.mda_count) == 2
    assert int(pv.mda_used_count) == 2

    # The rest of the pvs will have 2 unused metadata areas.
    for dev in dev2, dev3:
        pv = lvm.getPV(dev)
        assert pv.name == dev
        assert pv.vg_name == vg_name
        assert int(pv.dev_size) == dev_size
        assert int(pv.mda_count) == 2
        assert int(pv.mda_used_count) == 0

    lvm.removeVG(vg_name)

    # We remove the VG
    with pytest.raises(se.VolumeGroupDoesNotExist):
        lvm.getVG(vg_name)

    # But keep the PVs, not sure why.
    for dev in dev1, dev2, dev3:
        pv = lvm.getPV(dev)
        assert pv.name == dev
        assert pv.vg_name == ""


@requires_root
@xfail_python3
@pytest.mark.root
def test_vg_extend_reduce(tmp_storage):
    dev_size = 10 * 1024**3
    dev1 = tmp_storage.create_device(dev_size)
    dev2 = tmp_storage.create_device(dev_size)
    dev3 = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev1], "initial-tag", 128)

    vg = lvm.getVG(vg_name)
    assert vg.pv_name == (dev1,)

    lvm.extendVG(vg_name, [dev2, dev3], force=False)
    vg = lvm.getVG(vg_name)
    assert sorted(vg.pv_name) == sorted((dev1, dev2, dev3))

    # The first pv (metadata pv) will have the 2 used metadata areas.
    pv = lvm.getPV(dev1)
    assert pv.name == dev1
    assert pv.vg_name == vg_name
    assert int(pv.dev_size) == dev_size
    assert int(pv.mda_count) == 2
    assert int(pv.mda_used_count) == 2

    # The rest of the pvs will have 2 unused metadata areas.
    for dev in dev2, dev3:
        pv = lvm.getPV(dev)
        assert pv.name == dev
        assert pv.vg_name == vg_name
        assert int(pv.dev_size) == dev_size
        assert int(pv.mda_count) == 2
        assert int(pv.mda_used_count) == 0

    lvm.reduceVG(vg_name, dev2)
    vg = lvm.getVG(vg_name)
    assert sorted(vg.pv_name) == sorted((dev1, dev3))

    lvm.removeVG(vg_name)
    with pytest.raises(se.VolumeGroupDoesNotExist):
        lvm.getVG(vg_name)


@requires_root
@xfail_python3
@pytest.mark.root
def test_vg_add_delete_tags(tmp_storage):
    dev_size = 20 * 1024**3
    dev = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev], "initial-tag", 128)
    lvm.changeVGTags(
        vg_name,
        delTags=("initial-tag",),
        addTags=("new-tag-1", "new-tag-2"))

    lvm.changeVGTags(
        vg_name,
        delTags=["initial-tag"],
        addTags=["new-tag-1", "new-tag-2"])
    vg = lvm.getVG(vg_name)
    assert sorted(vg.tags) == ["new-tag-1", "new-tag-2"]


@requires_root
@pytest.mark.root
def test_vg_check(tmp_storage):
    dev_size = 10 * 1024**3
    dev1 = tmp_storage.create_device(dev_size)
    dev2 = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev1, dev2], "initial-tag", 128)
    assert lvm.chkVG(vg_name)


@requires_root
@xfail_python3
@pytest.mark.root
def test_lv_create_remove(tmp_storage):
    dev_size = 10 * 1024**3
    dev1 = tmp_storage.create_device(dev_size)
    dev2 = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev1, dev2], "initial-tag", 128)

    lv_any = "lv-on-any-device"
    lv_specific = "lv-on-device-2"

    # Create the first LV on any device.
    lvm.createLV(vg_name, lv_any, 1024)
    lv = lvm.getLV(vg_name, lv_any)
    assert lv.name == lv_any
    assert lv.vg_name == vg_name
    assert int(lv.size) == 1024**3
    assert lv.tags == ()
    assert lv.writeable
    assert not lv.opened
    assert lv.active

    # LV typically created on dev1.
    device, extent = lvm.getFirstExt(vg_name, lv_any)
    assert device in dev1, dev2
    assert extent == "0"

    # Create the second LV on dev2.
    lvm.createLV(vg_name, lv_specific, 1024, device=dev2)
    device, extent = lvm.getFirstExt(vg_name, lv_specific)
    assert device == dev2

    # Remove both LVs.
    lvm.removeLVs(vg_name, [lv_any, lv_specific])
    for lv_name in (lv_any, lv_specific):
        with pytest.raises(se.LogicalVolumeDoesNotExistError):
            lvm.getLV(vg_name, lv_name)


@requires_root
@xfail_python3
@pytest.mark.root
def test_lv_add_delete_tags(tmp_storage):
    dev_size = 20 * 1024**3
    dev = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lv_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev], "initial-tag", 128)
    lvm.createLV(vg_name, lv_name, 1024, activate=False)
    lvm.changeLVTags(
        vg_name,
        lv_name,
        delTags=("initial-tag",),
        addTags=("new-tag-1", "new-tag-2"))

    lv = lvm.getLV(vg_name, lv_name)
    assert sorted(lv.tags) == ["new-tag-1", "new-tag-2"]


@requires_root
@xfail_python3
@pytest.mark.root
def test_lv_activate_deactivate(tmp_storage):
    dev_size = 20 * 1024**3
    dev = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lv_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev], "initial-tag", 128)
    lvm.createLV(vg_name, lv_name, 1024, activate=False)

    lv = lvm.getLV(vg_name, lv_name)
    assert not lv.active

    # Activate the inactive lv.
    lvm.activateLVs(vg_name, [lv_name])
    lv = lvm.getLV(vg_name, lv_name)
    assert lv.active

    # Deactivate the active lv.
    lvm.deactivateLVs(vg_name, [lv_name])
    lv = lvm.getLV(vg_name, lv_name)
    assert not lv.active


@requires_root
@xfail_python3
@pytest.mark.root
def test_lv_extend_reduce(tmp_storage):
    dev_size = 20 * 1024**3
    dev = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lv_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev], "initial-tag", 128)
    lvm.createLV(vg_name, lv_name, 1024)

    lvm.extendLV(vg_name, lv_name, 2048)
    lv = lvm.getLV(vg_name, lv_name)
    assert int(lv.size) == 2 * 1024**3

    # Reducing active LV requires force.
    lvm.reduceLV(vg_name, lv_name, 1024, force=True)
    lv = lvm.getLV(vg_name, lv_name)
    assert int(lv.size) == 1 * 1024**3


@requires_root
@xfail_python3
@pytest.mark.root
def test_lv_refresh(tmp_storage):
    dev_size = 20 * 1024**3
    dev = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lv_name = str(uuid.uuid4())
    lv_fullname = "{}/{}".format(vg_name, lv_name)
    lvm.createVG(vg_name, [dev], "initial-tag", 128)
    lvm.createLV(vg_name, lv_name, 1024)

    # Simulate extending the LV on the SPM.
    commands.run([
        "lvextend",
        "--config", tmp_storage.lvm_config(),
        "-L+1g",
        lv_fullname
    ])

    # Refreshing LV invalidates the cache to pick up changes from storage.
    lvm.refreshLVs(vg_name, [lv_name])
    lv = lvm.getLV(vg_name, lv_name)
    assert int(lv.size) == 2 * 1024**3

    # Simulate extending the LV on the SPM.
    commands.run([
        "lvextend",
        "--config", tmp_storage.lvm_config(),
        "-L+1g",
        lv_fullname
    ])

    # Activate active LV refreshes it.
    lvm.activateLVs(vg_name, [lv_name])
    lv = lvm.getLV(vg_name, lv_name)
    assert int(lv.size) == 3 * 1024**3


@requires_root
@xfail_python3
@pytest.mark.root
def test_lv_rename(tmp_storage):
    dev_size = 20 * 1024**3
    dev = tmp_storage.create_device(dev_size)
    vg_name = str(uuid.uuid4())
    lv_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev], "initial-tag", 128)
    lvm.createLV(vg_name, lv_name, 1024)

    new_lv_name = "renamed-" + lv_name
    lvm.renameLV(vg_name, lv_name, new_lv_name)
    lv = lvm.getLV(vg_name, new_lv_name)
    assert lv.name == new_lv_name


@requires_root
@xfail_python3
@pytest.mark.root
def test_bootstrap(tmp_storage):
    dev_size = 20 * 1024**3

    dev1 = tmp_storage.create_device(dev_size)
    vg1_name = str(uuid.uuid4())
    lvm.createVG(vg1_name, [dev1], "initial-tag", 128)

    dev2 = tmp_storage.create_device(dev_size)
    vg2_name = str(uuid.uuid4())
    lvm.createVG(vg2_name, [dev2], "initial-tag", 128)

    vgs = (vg1_name, vg2_name)

    for vg_name in vgs:
        # Create active lvs.
        for lv_name in ("skip", "prepared", "opened", "unused"):
            lvm.createLV(vg_name, lv_name, 1024)

        # Create links to prepared lvs.
        img_dir = os.path.join(sc.P_VDSM_STORAGE, vg_name, "img")
        os.makedirs(img_dir)
        os.symlink(
            lvm.lvPath(vg_name, "prepared"),
            os.path.join(img_dir, "prepared"))

    # Open some lvs during bootstrap.
    vg1_opened = lvm.lvPath(vg1_name, "opened")
    vg2_opened = lvm.lvPath(vg2_name, "opened")
    with open(vg1_opened), open(vg2_opened):

        lvm.bootstrap(skiplvs=["skip"])

        # Lvs in skiplvs, prepared lvs, and opened lvs should be active.
        for vg_name in vgs:
            for lv_name in ("skip", "prepared", "opened"):
                lv = lvm.getLV(vg_name, lv_name)
                assert lv.active

        # Unused lvs should not be active.
        for vg_name in vgs:
            lv = lvm.getLV(vg_name, "unused")
            assert not lv.active


@requires_root
@xfail_python3
@pytest.mark.root
def test_retry_with_wider_filter(tmp_storage):
    # Force reload of the cache. The system does not know about any device at
    # this point.
    lvm.getAllPVs()

    # Create a device - this device in not the lvm cached filter yet.
    dev = tmp_storage.create_device(20 * 1024**3)

    # We run vgcreate with explicit devices argument, so the filter is correct
    # and it succeeds.
    vg_name = str(uuid.uuid4())
    lvm.createVG(vg_name, [dev], "initial-tag", 128)

    # The cached filter is stale at this point, and so is the vg metadata in
    # the cache. Running "vgs vg-name" fails because of the stale filter, so we
    # invalidate the filter and run it again.

    vg = lvm.getVG(vg_name)
    assert vg.pv_name == (dev,)
