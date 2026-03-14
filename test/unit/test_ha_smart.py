"""Unit tests for smart HA handling: parsing, classification, and orchestration."""

import json
import os
import unittest
from unittest.mock import patch, MagicMock

from styx.wrappers import (
    _parse_ha_resources, _parse_ha_groups, _parse_ha_services_on_nodes,
)
from styx.orchestrate import _classify_ha_relocatable

_FIXTURES = os.path.join(os.path.dirname(__file__), '..', 'fixtures', 'pvesh')


def _load(name):
    with open(os.path.join(_FIXTURES, name)) as f:
        return json.load(f)


# ── _parse_ha_resources ──────────────────────────────────────────────────────

class TestParseHaResources(unittest.TestCase):

    def test_empty_list(self):
        self.assertEqual(_parse_ha_resources([]), [])

    def test_only_started_returned(self):
        result = _parse_ha_resources(_load('ha_resources.json'))
        sids = [r['sid'] for r in result]
        self.assertIn('vm:106', sids)
        self.assertIn('vm:104', sids)
        self.assertIn('vm:100', sids)
        self.assertIn('vm:110', sids)
        self.assertNotIn('vm:112', sids)  # stopped

    def test_group_preserved(self):
        result = _parse_ha_resources(_load('ha_resources.json'))
        by_sid = {r['sid']: r for r in result}
        self.assertEqual(by_sid['vm:100']['group'], 'pinned')
        self.assertEqual(by_sid['vm:106']['group'], 'anynode')

    def test_entries_without_sid_skipped(self):
        data = [{'state': 'started', 'group': 'grp1'}]
        self.assertEqual(_parse_ha_resources(data), [])

    def test_no_group_defaults_to_empty(self):
        data = [{'sid': 'vm:999', 'state': 'started'}]
        result = _parse_ha_resources(data)
        self.assertEqual(result[0]['group'], '')


# ── _parse_ha_groups ─────────────────────────────────────────────────────────

class TestParseHaGroups(unittest.TestCase):

    def test_empty_list(self):
        self.assertEqual(_parse_ha_groups([]), {})

    def test_fixture_groups(self):
        result = _parse_ha_groups(_load('ha_groups.json'))
        self.assertIn('pinned', result)
        self.assertIn('anynode', result)

    def test_restricted_flag(self):
        result = _parse_ha_groups(_load('ha_groups.json'))
        self.assertTrue(result['pinned']['restricted'])
        self.assertFalse(result['anynode']['restricted'])

    def test_nodes_parsed_as_set(self):
        result = _parse_ha_groups(_load('ha_groups.json'))
        self.assertEqual(result['pinned']['nodes'], {'pve2', 'pve1'})
        self.assertEqual(result['anynode']['nodes'], {'pve1', 'pve3', 'pve2', 'pve4'})

    def test_entries_without_group_key_skipped(self):
        data = [{'nodes': 'pve1', 'restricted': 0}]
        self.assertEqual(_parse_ha_groups(data), {})


# ── _parse_ha_services_on_nodes ──────────────────────────────────────────────

class TestParseHaServicesOnNodes(unittest.TestCase):

    def test_empty_data(self):
        self.assertEqual(_parse_ha_services_on_nodes([], {'pve1'}), [])

    def test_fixture_services_on_pve3(self):
        data = _load('ha_status_current.json')
        result = _parse_ha_services_on_nodes(data, {'pve3'})
        self.assertEqual(sorted(result), ['vm:104', 'vm:106'])

    def test_fixture_services_on_pve2(self):
        data = _load('ha_status_current.json')
        result = _parse_ha_services_on_nodes(data, {'pve2'})
        self.assertEqual(sorted(result), ['vm:100', 'vm:110'])

    def test_stopped_services_excluded(self):
        data = _load('ha_status_current.json')
        result = _parse_ha_services_on_nodes(data, {'pve4'})
        # vm:112 is stopped on pve4
        self.assertEqual(result, [])

    def test_non_service_entries_excluded(self):
        data = _load('ha_status_current.json')
        # pve1 only has quorum/lrm entries, no services
        result = _parse_ha_services_on_nodes(data, {'pve1'})
        self.assertEqual(result, [])

    def test_multiple_nodes(self):
        data = _load('ha_status_current.json')
        result = _parse_ha_services_on_nodes(data, {'pve2', 'pve3'})
        self.assertEqual(sorted(result), ['vm:100', 'vm:104', 'vm:106', 'vm:110'])


# ── _classify_ha_relocatable ─────────────────────────────────────────────────

class TestClassifyHaRelocatable(unittest.TestCase):

    def _make_ops(self, resources, groups):
        ops = MagicMock()
        ops.get_ha_resources.return_value = resources
        ops.get_ha_groups.return_value = groups
        return ops

    def test_non_restricted_group_is_relocatable(self):
        resources = [{'sid': 'vm:104', 'group': 'anynode', 'state': 'started', 'type': 'vm'}]
        groups = {'anynode': {'nodes': {'pve1', 'pve2', 'pve3'}, 'restricted': False}}
        ops = self._make_ops(resources, groups)

        relocatable, disable = _classify_ha_relocatable(ops, {'pve1'})
        self.assertEqual(relocatable, ['vm:104'])
        self.assertEqual(disable, [])

    def test_restricted_group_with_survivors_is_relocatable(self):
        resources = [{'sid': 'vm:100', 'group': 'pinned', 'state': 'started', 'type': 'vm'}]
        groups = {'pinned': {'nodes': {'pve1', 'pve2'}, 'restricted': True}}
        ops = self._make_ops(resources, groups)

        # Shutting down pve1 only — pve2 survives
        relocatable, disable = _classify_ha_relocatable(ops, {'pve1'})
        self.assertEqual(relocatable, ['vm:100'])
        self.assertEqual(disable, [])

    def test_restricted_group_all_down_is_non_relocatable(self):
        resources = [{'sid': 'vm:100', 'group': 'pinned', 'state': 'started', 'type': 'vm'}]
        groups = {'pinned': {'nodes': {'pve1', 'pve2'}, 'restricted': True}}
        ops = self._make_ops(resources, groups)

        # Shutting down both pve1 and pve2 — no survivors
        relocatable, disable = _classify_ha_relocatable(ops, {'pve1', 'pve2'})
        self.assertEqual(relocatable, [])
        self.assertEqual(disable, ['vm:100'])

    def test_no_group_is_relocatable(self):
        resources = [{'sid': 'vm:200', 'group': '', 'state': 'started', 'type': 'vm'}]
        groups = {}
        ops = self._make_ops(resources, groups)

        relocatable, disable = _classify_ha_relocatable(ops, {'pve1'})
        self.assertEqual(relocatable, ['vm:200'])
        self.assertEqual(disable, [])

    def test_unknown_group_is_relocatable(self):
        resources = [{'sid': 'vm:200', 'group': 'deleted_group', 'state': 'started', 'type': 'vm'}]
        groups = {}
        ops = self._make_ops(resources, groups)

        relocatable, disable = _classify_ha_relocatable(ops, {'pve1'})
        self.assertEqual(relocatable, ['vm:200'])
        self.assertEqual(disable, [])

    def test_mixed_classification_with_fixtures(self):
        """Use fixture data: shut down pve1 and pve2 (pinned group covers both)."""
        resources = _parse_ha_resources(_load('ha_resources.json'))
        groups = _parse_ha_groups(_load('ha_groups.json'))
        ops = self._make_ops(resources, groups)

        # pinned group = {pve1, pve2} restricted — shutting down both → non-relocatable
        # anynode group = {pve1,pve2,pve3,pve4} non-restricted → relocatable
        relocatable, disable = _classify_ha_relocatable(ops, {'pve1', 'pve2'})
        self.assertIn('vm:100', disable)
        for sid in ['vm:106', 'vm:104', 'vm:110']:
            self.assertIn(sid, relocatable)
        # vm:112 is stopped, should not appear in either list
        all_sids = relocatable + disable
        self.assertNotIn('vm:112', all_sids)

    def test_partial_shutdown_pinned_one_survivor(self):
        """Shut down only pve2 — pinned group has pve1 surviving."""
        resources = _parse_ha_resources(_load('ha_resources.json'))
        groups = _parse_ha_groups(_load('ha_groups.json'))
        ops = self._make_ops(resources, groups)

        relocatable, disable = _classify_ha_relocatable(ops, {'pve2'})
        # pinned={pve1,pve2}, pve1 survives → relocatable
        self.assertIn('vm:100', relocatable)
        self.assertEqual(disable, [])


if __name__ == '__main__':
    unittest.main()
