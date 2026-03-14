"""Unit tests for styx.config"""

import os
import tempfile
import unittest

from styx.config import load_config, DEFAULT_CEPH_FLAGS


def _write(content):
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False)
    f.write(content)
    f.close()
    return f.name


class TestDefaults(unittest.TestCase):

    def test_missing_file_returns_defaults(self):
        cfg = load_config('/nonexistent/path')
        self.assertEqual(cfg.timeout_drain, 120)
        self.assertEqual(cfg.timeout_vm, 120)
        self.assertEqual(cfg.ceph_flags, DEFAULT_CEPH_FLAGS)

    def test_noup_not_in_default_flags(self):
        cfg = load_config('/nonexistent/path')
        self.assertNotIn('noup', cfg.ceph_flags)


class TestHosts(unittest.TestCase):

    def test_parse_hosts_section(self):
        p = _write('[hosts]\npve1 = 10.0.0.1\npve2 = 10.0.0.2\n')
        try:
            cfg = load_config(p)
            self.assertEqual(cfg.hosts['pve1'], '10.0.0.1')
            self.assertEqual(cfg.hosts['pve2'], '10.0.0.2')
        finally:
            os.unlink(p)


class TestOrchestrator(unittest.TestCase):

    def test_parse_orchestrator(self):
        p = _write('[orchestrator]\nhost = pve1\n')
        try:
            self.assertEqual(load_config(p).orchestrator, 'pve1')
        finally:
            os.unlink(p)


class TestKubernetes(unittest.TestCase):

    def test_workers_and_cp(self):
        p = _write('[kubernetes]\nworkers = 211, 212\ncontrol_plane = 201\n')
        try:
            cfg = load_config(p)
            self.assertEqual(cfg.workers, ['211', '212'])
            self.assertEqual(cfg.control_plane, ['201'])
        finally:
            os.unlink(p)

    def test_server_token_ca_cert(self):
        p = _write(
            '[kubernetes]\n'
            'server = https://10.0.0.100:6443\n'
            'token = /etc/styx/token\n'
            'ca_cert = /etc/styx/ca.crt\n'
        )
        try:
            cfg = load_config(p)
            self.assertEqual(cfg.k8s_server,  'https://10.0.0.100:6443')
            self.assertEqual(cfg.k8s_token,   '/etc/styx/token')
            self.assertEqual(cfg.k8s_ca_cert, '/etc/styx/ca.crt')
        finally:
            os.unlink(p)


class TestCeph(unittest.TestCase):

    def test_enabled_and_flags(self):
        p = _write('[ceph]\nenabled = true\nflags = noout, norebalance\n')
        try:
            cfg = load_config(p)
            self.assertTrue(cfg.ceph_enabled)
            self.assertEqual(cfg.ceph_flags, ['noout', 'norebalance'])
        finally:
            os.unlink(p)

    def test_disabled(self):
        p = _write('[ceph]\nenabled = false\n')
        try:
            self.assertFalse(load_config(p).ceph_enabled)
        finally:
            os.unlink(p)

    def test_ceph_enabled_none_when_absent(self):
        p = _write('[timeouts]\ndrain = 60\n')
        try:
            self.assertIsNone(load_config(p).ceph_enabled)
        finally:
            os.unlink(p)


class TestTimeouts(unittest.TestCase):

    def test_parse_timeouts(self):
        p = _write('[timeouts]\ndrain = 60\nvm = 90\n')
        try:
            cfg = load_config(p)
            self.assertEqual(cfg.timeout_drain, 60)
            self.assertEqual(cfg.timeout_vm,    90)
        finally:
            os.unlink(p)

    def test_maintenance_multiplier_default(self):
        cfg = load_config('/nonexistent/path')
        self.assertEqual(cfg.maintenance_multiplier, 10)

    def test_maintenance_multiplier_parsed(self):
        p = _write('[timeouts]\nmaintenance_multiplier = 5\n')
        try:
            self.assertEqual(load_config(p).maintenance_multiplier, 5)
        finally:
            os.unlink(p)

    def test_maintenance_multiplier_with_other_timeouts(self):
        p = _write('[timeouts]\ndrain = 60\nvm = 90\nmaintenance_multiplier = 3\n')
        try:
            cfg = load_config(p)
            self.assertEqual(cfg.timeout_drain, 60)
            self.assertEqual(cfg.timeout_vm, 90)
            self.assertEqual(cfg.maintenance_multiplier, 3)
        finally:
            os.unlink(p)


class TestComments(unittest.TestCase):

    def test_inline_comments_stripped(self):
        p = _write('[timeouts]\ndrain = 60 # fast\nvm = 90 # generous\n')
        try:
            cfg = load_config(p)
            self.assertEqual(cfg.timeout_drain, 60)
            self.assertEqual(cfg.timeout_vm,    90)
        finally:
            os.unlink(p)

    def test_blank_and_comment_lines_ignored(self):
        p = _write('\n# comment\n\n[timeouts]\ndrain = 45\n')
        try:
            self.assertEqual(load_config(p).timeout_drain, 45)
        finally:
            os.unlink(p)


if __name__ == '__main__':
    unittest.main()
