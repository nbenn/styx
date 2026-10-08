"""Unit tests for styx.release_mounts."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from styx import release_mounts
from styx.release_mounts import network_mounts, run, _umount

_MOUNTS = """\
/dev/mapper/pve-root / ext4 rw,relatime 0 0
10.47.70.45:/mnt/pool/backup /mnt/pve/backup nfs4 rw,hard,vers=4.2 0 0
10.47.70.35,10.47.70.36:/ /mnt/pve/cephfs ceph rw,name=admin 0 0
//nas/share /mnt/pve/smb\\040share cifs rw 0 0
//nas/x /mnt/pve/smb\\040share/nested cifs rw 0 0
nas:/other /mnt/other nfs rw 0 0
/dev/sdb1 /mnt/pve/localdisk ext4 rw 0 0
"""


class TestNetworkMounts(unittest.TestCase):

    def test_selects_network_mounts_under_mnt_pve_deepest_first(self):
        self.assertEqual(network_mounts(_MOUNTS), [
            '/mnt/pve/smb share/nested',
            '/mnt/pve/smb share',
            '/mnt/pve/backup',
            '/mnt/pve/cephfs',
        ])

    def test_keep_paths_protect_their_mount(self):
        targets = network_mounts(_MOUNTS, keep_paths=['/mnt/pve/cephfs/styx/styx.pyz'])
        self.assertNotIn('/mnt/pve/cephfs', targets)
        self.assertIn('/mnt/pve/backup', targets)

    def test_keep_path_prefix_is_not_a_match(self):
        targets = network_mounts(_MOUNTS, keep_paths=['/mnt/pve/backupx/styx.pyz'])
        self.assertIn('/mnt/pve/backup', targets)

    def test_nothing_mounted(self):
        self.assertEqual(network_mounts('/dev/sda1 / ext4 rw 0 0\n'), [])


class TestUmount(unittest.TestCase):

    def _with(self, shell):
        real = release_mounts.subprocess.Popen
        def fake(argv, **kw):
            return real(['sh', '-c', shell], **kw)
        return mock.patch.object(release_mounts.subprocess, 'Popen', side_effect=fake)

    def test_success(self):
        with self._with('exit 0'):
            self.assertEqual(_umount('/mnt/pve/x'), 'unmounted')

    def test_busy_left_mounted(self):
        with self._with('echo "target is busy" >&2; exit 32'):
            self.assertEqual(_umount('/mnt/pve/x'), 'left mounted (target is busy)')

    def test_hanging_umount_abandoned(self):
        with self._with('sleep 5'):
            self.assertIn('timed out', _umount('/mnt/pve/x', timeout=0.5))


class TestRun(unittest.TestCase):

    def setUp(self):
        fd, self.mounts = tempfile.mkstemp()
        with os.fdopen(fd, 'w') as f:
            f.write(_MOUNTS)

    def tearDown(self):
        os.unlink(self.mounts)

    def test_dry_run_changes_nothing(self):
        with mock.patch.object(release_mounts.subprocess, 'run') as sp_run, \
             mock.patch.object(release_mounts, '_umount') as um:
            with redirect_stdout(io.StringIO()) as out:
                run(dry_run=True, mounts_path=self.mounts)
        sp_run.assert_not_called()
        um.assert_not_called()
        self.assertIn('/mnt/pve/backup', out.getvalue())

    def test_stops_pvestatd_before_unmounting(self):
        calls = []
        def sp_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stderr='')
        with mock.patch.object(release_mounts.subprocess, 'run', side_effect=sp_run), \
             mock.patch.object(release_mounts, '_umount',
                               side_effect=lambda t: calls.append(['umount', t]) or 'unmounted'):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(run(mounts_path=self.mounts), 0)
        self.assertEqual(calls[0], ['systemctl', 'stop', 'pvestatd'])
        self.assertEqual([c[1] for c in calls[1:]],
                         network_mounts(_MOUNTS))


if __name__ == '__main__':
    unittest.main()
