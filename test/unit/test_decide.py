"""Unit tests for styx.decide"""

import unittest

from styx.decide import (
    should_disable_ha, should_run_polling,
    should_poweroff_hosts, should_set_ceph_flags,
)


class TestPhasePredicates(unittest.TestCase):

    def test_disable_ha_phase_1_no(self):
        self.assertFalse(should_disable_ha(1))

    def test_disable_ha_phase_2_yes(self):
        self.assertTrue(should_disable_ha(2))

    def test_disable_ha_phase_3_yes(self):
        self.assertTrue(should_disable_ha(3))

    def test_run_polling_phase_1_no(self):
        self.assertFalse(should_run_polling(1))

    def test_run_polling_phase_2_yes(self):
        self.assertTrue(should_run_polling(2))

    def test_poweroff_hosts_phase_2_no(self):
        self.assertFalse(should_poweroff_hosts(2))

    def test_poweroff_hosts_phase_3_yes(self):
        self.assertTrue(should_poweroff_hosts(3))

    def test_ceph_flags_phase_2_no(self):
        self.assertFalse(should_set_ceph_flags(2))

    def test_ceph_flags_phase_3_yes(self):
        self.assertTrue(should_set_ceph_flags(3))


if __name__ == '__main__':
    unittest.main()
