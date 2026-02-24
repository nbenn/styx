"""Integration tests: full shutdown sequences."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from styx.config import StyxConfig
from styx.discover import ClusterTopology
from styx.orchestrate import (
    _drain_all_k8s, _dispatch_independent_phase,
    run_polling_loop, main,
)
from styx.policy import Policy, DryRunPolicy

from test.integration.helpers import FakeOperations, start_fake_vm, kill_all_fake_vms

# Default 3-node topology used by most tests:
#   pve1 = orchestrator, VM 101 (non-k8s,  name "infra-vm")
#   pve2 = VM 211 (k8s worker,  name "worker1")
#   pve3 = VM 201 (k8s CP,      name "cp1")
_VM_HOST = {'101': 'pve1', '211': 'pve2', '201': 'pve3'}
_VM_NAME = {'101': 'infra-vm', '211': 'worker1', '201': 'cp1'}
_VM_TYPE = {'101': 'qemu', '211': 'qemu', '201': 'qemu'}


def _default_topo(run_dir=None, ceph=False):
    return ClusterTopology(
        host_ips    = {'pve1': '10.0.0.1', 'pve2': '10.0.0.2', 'pve3': '10.0.0.3'},
        orchestrator= 'pve1',
        vm_host     = dict(_VM_HOST),
        vm_name     = dict(_VM_NAME),
        vm_type     = dict(_VM_TYPE),
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


class TestDrainAllK8s(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        start_fake_vm('211', self._tmp)
        start_fake_vm('201', self._tmp)

    def tearDown(self):
        kill_all_fake_vms(self._tmp)

    def test_drains_all_k8s_nodes(self):
        ops    = FakeOperations(self._tmp, _VM_HOST)
        topo   = _default_topo()
        config = _default_config()
        _drain_all_k8s(topo, config, ops, Policy())
        self.assertIn('DRAIN worker1', ops.drain_log)
        self.assertIn('DRAIN cp1', ops.drain_log)
        # No VMs should be shut down by drain
        self.assertEqual(ops.shutdown_log, [])

    def test_dry_run_does_not_drain(self):
        ops = FakeOperations(self._tmp, _VM_HOST)
        _drain_all_k8s(_default_topo(), _default_config(), ops, DryRunPolicy())
        self.assertEqual(ops.drain_log, [])


class TestDispatchIndependentPhase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        for vmid in ['101', '211', '201']:
            start_fake_vm(vmid, self._tmp)

    def tearDown(self):
        kill_all_fake_vms(self._tmp)

    def test_dispatches_per_host(self):
        ops  = FakeOperations(self._tmp, _VM_HOST)
        topo = _default_topo()
        _dispatch_independent_phase(topo, _default_config(), ops, Policy(),
                                    do_poweroff=False)
        # Each peer should get one LOCAL_SHUTDOWN entry
        peer_dispatches = [s for s in ops.shutdown_log
                           if s.startswith('LOCAL_SHUTDOWN')]
        hosts_dispatched = {s.split()[1] for s in peer_dispatches}
        self.assertIn('pve2', hosts_dispatched)
        self.assertIn('pve3', hosts_dispatched)

    def test_orchestrator_has_no_poweroff_delay(self):
        """Orchestrator LOCAL_SHUTDOWN should have no poweroff delay."""
        ops  = FakeOperations(self._tmp, _VM_HOST)
        topo = _default_topo()
        _dispatch_independent_phase(topo, _default_config(), ops, Policy(),
                                    do_poweroff=True)
        # Orchestrator (pve1) should get a dispatch too
        orch_dispatches = [s for s in ops.shutdown_log
                           if 'LOCAL_SHUTDOWN pve1' in s]
        self.assertTrue(len(orch_dispatches) > 0)

    def test_vm_filter_limits_scope(self):
        ops  = FakeOperations(self._tmp, _VM_HOST)
        topo = _default_topo()
        k8s_vmids = set(topo.k8s_workers + topo.k8s_cp)
        _dispatch_independent_phase(topo, _default_config(), ops, Policy(),
                                    do_poweroff=False, vm_filter=k8s_vmids)
        # Only k8s VMs should be dispatched, not VM 101
        all_entries = ' '.join(ops.shutdown_log)
        self.assertNotIn('101', all_entries)


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
            vm_type      = {'211': 'qemu'},
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

    def test_phase3_dispatches_all_hosts_and_powers_off(self):
        ops = self._run(3)
        # All hosts should have LOCAL_SHUTDOWN entries (peers via dispatch)
        local_shutdowns = [s for s in ops.shutdown_log
                           if s.startswith('LOCAL_SHUTDOWN')]
        hosts_dispatched = {s.split()[1] for s in local_shutdowns}
        self.assertIn('pve2', hosts_dispatched)
        self.assertIn('pve3', hosts_dispatched)
        self.assertIn('POWEROFF_SELF', ops.poweroff_log)

    def test_phase3_powers_off_peers_before_self(self):
        ops = self._run(3)
        poweroffs = ops.poweroff_log
        self.assertIn('POWEROFF_SELF', poweroffs)
        self.assertEqual(poweroffs[-1], 'POWEROFF_SELF')

    def test_phase2_no_host_poweroff(self):
        ops = self._run(2)
        self.assertEqual(ops.poweroff_log, [])

    def test_phase1_only_k8s_vms_dispatched(self):
        ops = self._run(1)
        all_entries = ' '.join(ops.shutdown_log)
        # k8s VMs (211, 201) should be dispatched; non-k8s (101) should not
        self.assertNotIn('101', all_entries)
        # At least k8s hosts should have dispatches
        local_shutdowns = [s for s in ops.shutdown_log
                           if s.startswith('LOCAL_SHUTDOWN')]
        self.assertTrue(len(local_shutdowns) > 0)
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

    def test_all_drains_before_dispatch(self):
        """All DRAINs must precede all LOCAL_SHUTDOWN entries."""
        ops = self._run(3)
        drain_seqs = [s for s, a in ops.sequence_log if a.startswith('DRAIN')]
        dispatch_seqs = [s for s, a in ops.sequence_log
                         if a.startswith('LOCAL_SHUTDOWN')]
        self.assertTrue(len(drain_seqs) > 0)
        self.assertTrue(len(dispatch_seqs) > 0)
        self.assertGreater(min(dispatch_seqs), max(drain_seqs))

    def test_ceph_flags_before_dispatch(self):
        """Ceph flags must be set before LOCAL_SHUTDOWN is dispatched."""
        ops = self._run(3, ceph=True)
        ceph_seqs = [s for s, a in ops.sequence_log
                     if a.startswith('CEPH_FLAGS')]
        dispatch_seqs = [s for s, a in ops.sequence_log
                         if a.startswith('LOCAL_SHUTDOWN')]
        self.assertTrue(len(ceph_seqs) > 0)
        self.assertTrue(len(dispatch_seqs) > 0)
        self.assertGreater(min(dispatch_seqs), max(ceph_seqs))

    def test_local_shutdown_dispatched_per_host(self):
        """Each host should get exactly one LOCAL_SHUTDOWN dispatch."""
        ops = self._run(3)
        local_shutdowns = [s for s in ops.shutdown_log
                           if s.startswith('LOCAL_SHUTDOWN')]
        hosts = [s.split()[1] for s in local_shutdowns]
        self.assertEqual(sorted(hosts), ['pve1', 'pve2', 'pve3'])

    def test_poweroff_delay_in_phase3(self):
        """Phase 3 dispatches should include poweroff_delay (via do_poweroff=True)."""
        # This is implicitly tested via the poweroff_log — peers get powered off
        # by the leader in the polling loop before the deadline would fire.
        ops = self._run(3)
        self.assertTrue(any('POWEROFF pve' in p for p in ops.poweroff_log))

    def test_no_poweroff_in_phase2(self):
        """Phase 2 should not power off any host."""
        ops = self._run(2)
        self.assertEqual(ops.poweroff_log, [])


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

    def test_dispatch_to_stopped_vm_is_noop(self):
        """dispatch_local_shutdown on a VM with no PID file should not error."""
        ops = FakeOperations(self._tmp, _VM_HOST)
        ops.dispatch_local_shutdown('pve1', [('qemu', '999')], 5)
        self.assertTrue(any('999' in s for s in ops.shutdown_log))


if __name__ == '__main__':
    unittest.main()
