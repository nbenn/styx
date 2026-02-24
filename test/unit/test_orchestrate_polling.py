"""Unit tests for orchestrate.run_polling_loop edge cases, _disable_ha,
_log_revert_summary, and _log_startup_checklist."""

import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout

from styx.discover import ClusterTopology
from styx.orchestrate import (
    run_polling_loop, _disable_ha, _log_revert_summary, _log_startup_checklist,
)
from styx.policy import Policy, DryRunPolicy

from test.integration.helpers import FakeOperations, start_fake_vm, kill_all_fake_vms


def _topo(**kwargs):
    defaults = dict(
        host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
        orchestrator='pve1',
        vm_host={},
        vm_name={},
        vm_type={},
        k8s_workers=[],
        k8s_cp=[],
        k8s_enabled=False,
        ceph_enabled=False,
    )
    defaults.update(kwargs)
    return ClusterTopology(**defaults)


def _capture_log(fn):
    """Call fn() and return captured stdout (where log() writes)."""
    import styx.policy
    original = styx.policy._log_fh
    styx.policy._log_fh = None
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn()
    finally:
        styx.policy._log_fh = original
    return buf.getvalue()


class TestPollingLoopOrchestratorVMs(unittest.TestCase):
    """Edge cases: orchestrator VMs still running when peers are done."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        kill_all_fake_vms(self._tmp)

    def test_waits_for_orchestrator_vms_after_peers_done(self):
        """Peers done but orchestrator VM still running — loop keeps polling."""
        start_fake_vm('101', self._tmp)
        vm_host = {'101': 'pve1', '211': 'pve2'}
        ops = FakeOperations(self._tmp, vm_host)
        topo = _topo(
            vm_host=dict(vm_host),
            vm_name={'101': 'infra', '211': 'worker1'},
            vm_type={'101': 'qemu', '211': 'qemu'},
        )
        # pve2 has no running VM (no PID file for 211) — peer is "done"
        # pve1 has VM 101 running — orchestrator must wait

        def delayed_kill():
            time.sleep(0.15)
            kill_all_fake_vms(self._tmp)
        threading.Thread(target=delayed_kill, daemon=True).start()

        run_polling_loop(topo, ops, Policy(), do_poweroff=True, poll_interval=0.05)
        self.assertIn('POWEROFF pve2', ops.poweroff_log)

    def test_no_vms_single_host_exits_immediately(self):
        """Single-host cluster with no VMs exits without powering off anything."""
        ops = FakeOperations(self._tmp, {})
        topo = _topo(host_ips={'pve1': '10.0.0.1'})
        run_polling_loop(topo, ops, Policy(), do_poweroff=True, poll_interval=0.05)
        self.assertEqual(ops.poweroff_log, [])

    def test_no_vms_peers_get_powered_off(self):
        """Peers with no VMs are still powered off (empty host_vms)."""
        ops = FakeOperations(self._tmp, {})
        topo = _topo()  # pve1 + pve2, no VMs
        run_polling_loop(topo, ops, Policy(), do_poweroff=True, poll_interval=0.05)
        self.assertIn('POWEROFF pve2', ops.poweroff_log)

    def test_all_vms_on_orchestrator_exits_when_stopped(self):
        """Only orchestrator has VMs, single-host — loop exits once they stop."""
        start_fake_vm('101', self._tmp)
        vm_host = {'101': 'pve1'}
        ops = FakeOperations(self._tmp, vm_host)
        topo = _topo(
            host_ips={'pve1': '10.0.0.1'},
            vm_host=dict(vm_host),
            vm_name={'101': 'infra'},
            vm_type={'101': 'qemu'},
        )
        kill_all_fake_vms(self._tmp)
        run_polling_loop(topo, ops, Policy(), do_poweroff=True, poll_interval=0.05)
        self.assertEqual(ops.poweroff_log, [])

    def test_peer_not_powered_off_when_do_poweroff_false(self):
        """Even when VMs stop, peers are not powered off if do_poweroff=False."""
        vm_host = {'211': 'pve2'}
        ops = FakeOperations(self._tmp, vm_host)
        topo = _topo(
            vm_host=dict(vm_host),
            vm_name={'211': 'worker1'},
            vm_type={'211': 'qemu'},
        )
        run_polling_loop(topo, ops, Policy(), do_poweroff=False, poll_interval=0.05)
        self.assertEqual(ops.poweroff_log, [])

    def test_multiple_peers_powered_off_independently(self):
        """Each peer powers off as soon as its own VMs are done."""
        start_fake_vm('301', self._tmp)
        vm_host = {'211': 'pve2', '301': 'pve3'}
        ops = FakeOperations(self._tmp, vm_host)
        topo = _topo(
            host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2', 'pve3': '10.0.0.3'},
            vm_host=dict(vm_host),
            vm_name={'211': 'w1', '301': 'w2'},
            vm_type={'211': 'qemu', '301': 'qemu'},
        )
        # pve2's VM 211 already stopped (no PID file)
        # pve3's VM 301 running — kill after a moment
        def delayed_kill():
            time.sleep(0.15)
            kill_all_fake_vms(self._tmp)
        threading.Thread(target=delayed_kill, daemon=True).start()

        run_polling_loop(topo, ops, Policy(), do_poweroff=True, poll_interval=0.05)
        self.assertIn('POWEROFF pve2', ops.poweroff_log)
        self.assertIn('POWEROFF pve3', ops.poweroff_log)


class TestDisableHA(unittest.TestCase):
    """Tests for _disable_ha with various HA resource states."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        kill_all_fake_vms(self._tmp)

    def _make_ops(self, ha_sids, wait_result=True):
        vm_host = {'211': 'pve2', '201': 'pve3'}
        ops = FakeOperations(self._tmp, vm_host)
        ops.get_ha_started_sids = lambda: ha_sids
        ops.wait_ha_disabled = lambda sid, timeout=30: wait_result
        return ops

    def test_disables_matching_sids_scope_all(self):
        ops = self._make_ops(['vm:211', 'vm:201'])
        topo = _topo(
            vm_host={'211': 'pve2', '201': 'pve3'},
            vm_name={'211': 'w1', '201': 'cp1'},
            vm_type={'211': 'qemu', '201': 'qemu'},
        )
        _disable_ha(topo, ops, Policy(), 'all')
        self.assertIn('DISABLE_HA vm:211', ops.ha_log)
        self.assertIn('DISABLE_HA vm:201', ops.ha_log)

    def test_skips_sids_not_in_scope(self):
        ops = self._make_ops(['vm:999'])
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'w1'},
            vm_type={'211': 'qemu'},
        )
        _disable_ha(topo, ops, Policy(), 'all')
        self.assertEqual(ops.ha_log, [])

    def test_scope_k8s_only_targets_k8s_vms(self):
        ops = self._make_ops(['vm:211', 'vm:101'])
        topo = _topo(
            vm_host={'211': 'pve2', '101': 'pve1'},
            vm_name={'211': 'w1', '101': 'infra'},
            vm_type={'211': 'qemu', '101': 'qemu'},
            k8s_workers=['211'],
            k8s_enabled=True,
        )
        _disable_ha(topo, ops, Policy(), 'k8s')
        self.assertIn('DISABLE_HA vm:211', ops.ha_log)
        disabled_vms = [s.split()[-1] for s in ops.ha_log]
        self.assertNotIn('vm:101', disabled_vms)

    def test_dry_run_does_not_disable(self):
        ops = self._make_ops(['vm:211'])
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'w1'},
            vm_type={'211': 'qemu'},
        )
        _disable_ha(topo, ops, DryRunPolicy(), 'all')
        self.assertEqual(ops.ha_log, [])

    def test_wait_timeout_triggers_warning(self):
        """When wait_ha_disabled returns False, a warning is logged."""
        ops = self._make_ops(['vm:211'], wait_result=False)
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'w1'},
            vm_type={'211': 'qemu'},
        )
        _disable_ha(topo, ops, Policy(), 'all')
        self.assertIn('DISABLE_HA vm:211', ops.ha_log)

    def test_sid_without_colon_matched_directly(self):
        """SID like '211' (no vm: prefix) should still match vm_host keys."""
        ops = self._make_ops(['211'])
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'w1'},
            vm_type={'211': 'qemu'},
        )
        _disable_ha(topo, ops, Policy(), 'all')
        self.assertIn('DISABLE_HA 211', ops.ha_log)


class TestLogRevertSummary(unittest.TestCase):
    """Tests for _log_revert_summary (partial-run checklist)."""

    def test_includes_ceph_flags(self):
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'w1'},
            vm_type={'211': 'qemu'},
        )
        args = type('Args', (), {'skip_poweroff': False, 'hosts': ['pve2']})()
        output = _capture_log(lambda: _log_revert_summary(topo, args, ['noout']))
        self.assertIn('noout', output)
        self.assertIn('ceph osd unset', output)

    def test_includes_k8s_nodes(self):
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'worker1'},
            vm_type={'211': 'qemu'},
            k8s_workers=['211'],
        )
        args = type('Args', (), {'skip_poweroff': False, 'hosts': ['pve2']})()
        output = _capture_log(lambda: _log_revert_summary(topo, args, []))
        self.assertIn('worker1', output)
        self.assertIn('uncordon', output)

    def test_skip_poweroff_mentions_host_not_powered_off(self):
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'w1'},
            vm_type={'211': 'qemu'},
        )
        args = type('Args', (), {'skip_poweroff': True, 'hosts': ['pve2']})()
        output = _capture_log(lambda: _log_revert_summary(topo, args, []))
        self.assertIn('NOT powered off', output)

    def test_no_skip_poweroff_mentions_powered_off_hosts(self):
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'w1'},
            vm_type={'211': 'qemu'},
        )
        args = type('Args', (), {'skip_poweroff': False, 'hosts': ['pve2']})()
        output = _capture_log(lambda: _log_revert_summary(topo, args, []))
        self.assertIn('powered off', output.lower())

    def test_ceph_and_k8s_together(self):
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'worker1'},
            vm_type={'211': 'qemu'},
            k8s_workers=['211'],
        )
        args = type('Args', (), {'skip_poweroff': False, 'hosts': ['pve2']})()
        output = _capture_log(
            lambda: _log_revert_summary(topo, args, ['noout', 'norebalance']))
        self.assertIn('noout', output)
        self.assertIn('uncordon', output)


class TestLogStartupChecklist(unittest.TestCase):
    """Tests for _log_startup_checklist (full-run checklist)."""

    def test_no_flags_no_k8s_no_output(self):
        topo = _topo()
        output = _capture_log(lambda: _log_startup_checklist(topo, []))
        self.assertNotIn('startup checklist', output)

    def test_ceph_flags_in_checklist(self):
        topo = _topo()
        output = _capture_log(
            lambda: _log_startup_checklist(topo, ['noout', 'norebalance']))
        self.assertIn('noout', output)
        self.assertIn('norebalance', output)
        self.assertIn('ceph osd unset', output)

    def test_k8s_nodes_in_checklist(self):
        topo = _topo(
            vm_host={'211': 'pve2'},
            vm_name={'211': 'worker1'},
            k8s_workers=['211'],
        )
        output = _capture_log(lambda: _log_startup_checklist(topo, []))
        self.assertIn('worker1', output)
        self.assertIn('uncordon', output)


if __name__ == '__main__':
    unittest.main()
