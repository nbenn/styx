"""Integration tests: full shutdown sequences."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from styx.config import StyxConfig
from styx.discover import ClusterTopology
from styx.orchestrate import (
    run_k8s_track, run_other_vm_track, run_polling_loop, main,
)
from styx.policy import Policy, DryRunPolicy

from test.integration.helpers import FakeOperations, start_fake_vm, kill_all_fake_vms

# Default 3-node topology used by most tests:
#   pve1 = orchestrator, VM 101 (non-k8s,  name "infra-vm")
#   pve2 = VM 211 (k8s worker,  name "worker1")
#   pve3 = VM 201 (k8s CP,      name "cp1")
_VM_HOST = {'101': 'pve1', '211': 'pve2', '201': 'pve3'}
_VM_NAME = {'101': 'infra-vm', '211': 'worker1', '201': 'cp1'}


def _default_topo(run_dir=None, ceph=False):
    return ClusterTopology(
        host_ips    = {'pve1': '10.0.0.1', 'pve2': '10.0.0.2', 'pve3': '10.0.0.3'},
        orchestrator= 'pve1',
        vm_host     = dict(_VM_HOST),
        vm_name     = dict(_VM_NAME),
        k8s_workers = ['211'],
        k8s_cp      = ['201'],
        k8s_enabled = True,
        ceph_enabled= ceph,
    )


def _default_config():
    cfg = StyxConfig()
    cfg.timeout_drain = 5
    cfg.timeout_vm    = 5
    cfg.ceph_flags    = ['noout', 'norebalance']
    return cfg


class TestKubernetesTrack(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        start_fake_vm('211', self._tmp)
        start_fake_vm('201', self._tmp)

    def tearDown(self):
        kill_all_fake_vms(self._tmp)

    def _ops(self, topo=None):
        return FakeOperations(self._tmp, _VM_HOST)

    def test_drains_workers_before_cp(self):
        ops    = self._ops()
        topo   = _default_topo()
        config = _default_config()
        run_k8s_track(topo, config, ops, Policy())
        worker_idx = ops.drain_log.index('DRAIN worker1')
        cp_idx     = ops.drain_log.index('DRAIN cp1')
        self.assertLess(worker_idx, cp_idx)

    def test_drains_and_shuts_down_all_k8s_vms(self):
        ops = self._ops()
        run_k8s_track(_default_topo(), _default_config(), ops, Policy())
        self.assertIn('DRAIN worker1', ops.drain_log)
        self.assertIn('DRAIN cp1',     ops.drain_log)
        self.assertTrue(any('211' in s for s in ops.shutdown_log))
        self.assertTrue(any('201' in s for s in ops.shutdown_log))

    def test_dry_run_does_not_drain_or_shutdown(self):
        ops = self._ops()
        run_k8s_track(_default_topo(), _default_config(), ops, DryRunPolicy())
        self.assertEqual(ops.drain_log,    [])
        self.assertEqual(ops.shutdown_log, [])


class TestOtherVmTrack(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        start_fake_vm('101', self._tmp)

    def tearDown(self):
        kill_all_fake_vms(self._tmp)

    def test_shuts_down_non_k8s_vms(self):
        ops = FakeOperations(self._tmp, _VM_HOST)
        run_other_vm_track(_default_topo(), _default_config(), ops, Policy())
        self.assertTrue(any('101' in s for s in ops.shutdown_log))

    def test_does_not_touch_k8s_vms(self):
        ops = FakeOperations(self._tmp, _VM_HOST)
        run_other_vm_track(_default_topo(), _default_config(), ops, Policy())
        self.assertFalse(any('211' in s for s in ops.shutdown_log))
        self.assertFalse(any('201' in s for s in ops.shutdown_log))


class TestPollingLoop(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        kill_all_fake_vms(self._tmp)

    def test_powers_off_peer_when_vms_stop(self):
        start_fake_vm('211', self._tmp)
        ops  = FakeOperations(self._tmp, _VM_HOST)
        topo = ClusterTopology(
            host_ips     = {'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
            orchestrator = 'pve1',
            vm_host      = {'211': 'pve2'},
            vm_name      = {'211': 'worker1'},
            k8s_workers  = ['211'],
        )
        # Kill the VM first so polling sees it as stopped immediately
        kill_all_fake_vms(self._tmp)
        run_polling_loop(topo, ops, Policy(), do_poweroff=True, poll_interval=1)
        self.assertIn('POWEROFF pve2', ops.poweroff_log)

    def test_no_poweroff_when_disabled(self):
        ops  = FakeOperations(self._tmp, {'211': 'pve2'})
        topo = ClusterTopology(
            host_ips     = {'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
            orchestrator = 'pve1',
            vm_host      = {},   # no VMs — loop exits immediately
        )
        run_polling_loop(topo, ops, Policy(), do_poweroff=False, poll_interval=1)
        self.assertEqual(ops.poweroff_log, [])

    def test_dry_run_logs_but_does_not_poweroff(self):
        ops  = FakeOperations(self._tmp, {})
        topo = ClusterTopology(
            host_ips     = {'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
            orchestrator = 'pve1',
            vm_host      = {},
        )
        run_polling_loop(topo, ops, DryRunPolicy(), do_poweroff=True, poll_interval=1)
        self.assertEqual(ops.poweroff_log, [])


class TestMainPhaseControl(unittest.TestCase):
    """Test phase-gating via main() with injected fake discovery + ops."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        for vmid in ['101', '211', '201']:
            start_fake_vm(vmid, self._tmp)
        self._conf = tempfile.NamedTemporaryFile(
            mode='w', suffix='.conf', delete=False,
        )
        self._conf.write(
            '[hosts]\npve1 = 10.0.0.1\npve2 = 10.0.0.2\npve3 = 10.0.0.3\n'
            '[orchestrator]\nhost = pve1\n'
            '[kubernetes]\nworkers = 211\ncontrol_plane = 201\n'
            '[timeouts]\ndrain = 5\nvm = 5\n'
        )
        self._conf.close()

    def tearDown(self):
        kill_all_fake_vms(self._tmp)
        os.unlink(self._conf.name)

    def _run(self, phase, ceph=False):
        topo = _default_topo(self._tmp, ceph=ceph)
        ops  = FakeOperations(self._tmp, _VM_HOST)

        def fake_discover(config):
            return topo

        def fake_ops_factory(t, c):
            return ops

        os.environ['LOG_FILE'] = os.path.join(self._tmp, 'styx.log')
        os.environ['STYX_POLL_INTERVAL'] = '1'
        try:
            main(
                ['--phase', str(phase), '--config', self._conf.name],
                _discover_fn=fake_discover,
                _ops_factory=fake_ops_factory,
            )
        finally:
            os.environ.pop('LOG_FILE', None)
            os.environ.pop('STYX_POLL_INTERVAL', None)

        return ops

    def test_phase3_shuts_down_all_vms_and_powers_off(self):
        ops = self._run(3)
        self.assertTrue(any('101' in s for s in ops.shutdown_log))
        self.assertTrue(any('211' in s for s in ops.shutdown_log))
        self.assertTrue(any('201' in s for s in ops.shutdown_log))
        self.assertIn('POWEROFF_SELF', ops.poweroff_log)

    def test_phase3_powers_off_peers_before_self(self):
        ops = self._run(3)
        poweroffs = ops.poweroff_log
        self.assertIn('POWEROFF_SELF', poweroffs)
        self.assertEqual(poweroffs[-1], 'POWEROFF_SELF')

    def test_phase2_no_host_poweroff(self):
        ops = self._run(2)
        self.assertEqual(ops.poweroff_log, [])

    def test_phase1_only_k8s_vms_touched(self):
        ops = self._run(1)
        self.assertFalse(any('101' in s for s in ops.shutdown_log))
        self.assertTrue(
            any('211' in s for s in ops.shutdown_log) or
            any('201' in s for s in ops.shutdown_log)
        )
        self.assertEqual(ops.poweroff_log, [])

    def test_ceph_flags_set_in_phase3_when_enabled(self):
        ops = self._run(3, ceph=True)
        self.assertTrue(len(ops.ceph_log) > 0)

    def test_no_ceph_flags_when_disabled(self):
        ops = self._run(3, ceph=False)
        self.assertEqual(ops.ceph_log, [])

    def test_dry_run_no_side_effects(self):
        topo = _default_topo()
        ops  = FakeOperations(self._tmp, _VM_HOST)
        os.environ['LOG_FILE'] = os.path.join(self._tmp, 'styx.log')
        os.environ['STYX_POLL_INTERVAL'] = '1'
        try:
            main(
                ['--phase', '3', '--mode', 'dry-run', '--config', self._conf.name],
                _discover_fn=lambda c: topo,
                _ops_factory=lambda t, c: ops,
            )
        finally:
            os.environ.pop('LOG_FILE', None)
            os.environ.pop('STYX_POLL_INTERVAL', None)
        self.assertEqual(ops.shutdown_log, [])
        self.assertEqual(ops.poweroff_log, [])

    def test_workers_drained_before_cp(self):
        ops = self._run(3)
        worker_idx = ops.drain_log.index('DRAIN worker1')
        cp_idx     = ops.drain_log.index('DRAIN cp1')
        self.assertLess(worker_idx, cp_idx)


class TestIdempotency(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        kill_all_fake_vms(self._tmp)

    def test_phase1_then_phase3_completes(self):
        """Running phase 1 then phase 3 should not error."""
        for vmid in ['211', '201', '101']:
            start_fake_vm(vmid, self._tmp)

        topo = _default_topo(self._tmp)
        ops  = FakeOperations(self._tmp, _VM_HOST)

        conf = tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False)
        conf.write('[timeouts]\ndrain = 5\nvm = 5\n')
        conf.close()

        os.environ['LOG_FILE'] = os.path.join(self._tmp, 'styx.log')
        os.environ['STYX_POLL_INTERVAL'] = '1'
        try:
            for phase in (1, 3):
                main(
                    ['--phase', str(phase), '--config', conf.name],
                    _discover_fn=lambda c: _default_topo(self._tmp),
                    _ops_factory=lambda t, c: ops,
                )
        finally:
            os.environ.pop('LOG_FILE', None)
            os.environ.pop('STYX_POLL_INTERVAL', None)
            os.unlink(conf.name)

    def test_shutdown_vm_of_stopped_vm_is_noop(self):
        """FakeOperations.shutdown_vm on a VM with no PID file should not error."""
        ops = FakeOperations(self._tmp, _VM_HOST)
        ops.shutdown_vm('pve1', '999', 5)   # no PID file for 999
        self.assertTrue(any('999' in s for s in ops.shutdown_log))


if __name__ == '__main__':
    unittest.main()
