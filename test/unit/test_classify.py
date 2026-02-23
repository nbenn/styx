"""Unit tests for styx.classify"""

import unittest

from styx.classify import classify_vmid, other_vmids


class TestClassifyVmid(unittest.TestCase):

    def test_worker(self):
        self.assertEqual(classify_vmid('211', ['211'], []), 'k8s-worker')

    def test_cp(self):
        self.assertEqual(classify_vmid('201', [], ['201']), 'k8s-cp')

    def test_other(self):
        self.assertEqual(classify_vmid('101', ['211'], ['201']), 'other')

    def test_empty_lists(self):
        self.assertEqual(classify_vmid('101', [], []), 'other')


class TestOtherVmids(unittest.TestCase):

    def test_filters_k8s_vms(self):
        result = other_vmids(['101', '201', '211'], ['211'], ['201'])
        self.assertEqual(result, ['101'])

    def test_all_other_when_no_k8s(self):
        result = other_vmids(['101', '102'], [], [])
        self.assertEqual(set(result), {'101', '102'})

    def test_empty_when_all_k8s(self):
        result = other_vmids(['211', '201'], ['211'], ['201'])
        self.assertEqual(result, [])


if __name__ == '__main__':
    unittest.main()
