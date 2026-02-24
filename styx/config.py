"""styx.config — INI config parser.

All sections are optional; missing keys fall back to defaults.
"""

import configparser
from dataclasses import dataclass, field

# noup intentionally excluded: prevents OSDs coming back up after restart,
# which is a post-boot concern, not a shutdown concern.
DEFAULT_CEPH_FLAGS = ['noout', 'norecover', 'norebalance', 'nobackfill', 'nodown']

# For partial runs (--hosts): only noout is appropriate.  The recovery/
# rebalance/backfill flags are full-cluster-shutdown precautions; nodown
# masks real cluster state and should never be set during single-node
# maintenance.  noout alone tells Ceph the outage is intentional and
# prevents the mark-out timer from triggering data redistribution.
DEFAULT_CEPH_FLAGS_PARTIAL = ['noout']


@dataclass
class StyxConfig:
    hosts: dict = field(default_factory=dict)           # hostname -> IP
    orchestrator: str = ''
    workers: list = field(default_factory=list)         # VM IDs
    control_plane: list = field(default_factory=list)   # VM IDs
    k8s_server: str = ''
    k8s_token: str = ''
    k8s_ca_cert: str = ''
    ceph_enabled: object = None   # None = auto-detect, True/False = override
    ceph_flags: list = field(default_factory=lambda: list(DEFAULT_CEPH_FLAGS))
    ceph_flags_partial: list = field(default_factory=lambda: list(DEFAULT_CEPH_FLAGS_PARTIAL))
    timeout_drain: int = 120
    timeout_vm: int = 120


def _split_list(raw):
    """'a, b , c' or 'a b c' → ['a', 'b', 'c']"""
    return [x.strip() for x in raw.replace(',', ' ').split() if x.strip()]


def load_config(path):
    cfg = StyxConfig()
    parser = configparser.RawConfigParser(inline_comment_prefixes=('#',))
    if not parser.read(path):
        return cfg

    if parser.has_section('hosts'):
        for key, val in parser.items('hosts'):
            cfg.hosts[key.strip()] = val.strip()

    if parser.has_section('orchestrator'):
        cfg.orchestrator = parser.get('orchestrator', 'host', fallback='').strip()

    if parser.has_section('kubernetes'):
        raw = parser.get('kubernetes', 'workers', fallback='').strip()
        if raw:
            cfg.workers = _split_list(raw)
        raw = parser.get('kubernetes', 'control_plane', fallback='').strip()
        if raw:
            cfg.control_plane = _split_list(raw)
        cfg.k8s_server  = parser.get('kubernetes', 'server',   fallback='').strip()
        cfg.k8s_token   = parser.get('kubernetes', 'token',    fallback='').strip()
        cfg.k8s_ca_cert = parser.get('kubernetes', 'ca_cert',  fallback='').strip()

    if parser.has_section('ceph'):
        enabled = parser.get('ceph', 'enabled', fallback='').strip().lower()
        if enabled == 'true':
            cfg.ceph_enabled = True
        elif enabled == 'false':
            cfg.ceph_enabled = False
        raw = parser.get('ceph', 'flags', fallback='').strip()
        if raw:
            cfg.ceph_flags = _split_list(raw)
        raw = parser.get('ceph', 'partial_flags', fallback='').strip()
        if raw:
            cfg.ceph_flags_partial = _split_list(raw)

    if parser.has_section('timeouts'):
        raw = parser.get('timeouts', 'drain', fallback='').strip()
        if raw.isdigit():
            cfg.timeout_drain = int(raw)
        raw = parser.get('timeouts', 'vm', fallback='').strip()
        if raw.isdigit():
            cfg.timeout_vm = int(raw)

    return cfg
