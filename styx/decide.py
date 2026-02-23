"""styx.decide — Phase-gated decision predicates. Pure functions."""


def should_disable_ha(phase):
    """Disable HA for all resources when phase >= 2."""
    return phase >= 2


def should_run_polling(phase):
    """Run the VM polling loop when phase >= 2."""
    return phase >= 2


def should_poweroff_hosts(phase):
    """Power off hosts in the polling loop when phase >= 3."""
    return phase >= 3


def should_set_ceph_flags(phase):
    """Set Ceph OSD flags when phase >= 3."""
    return phase >= 3
