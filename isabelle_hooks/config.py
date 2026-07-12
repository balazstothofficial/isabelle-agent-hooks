"""Typed built-in policy and operational defaults for the Python guards."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GuardDefaults:
    """Central defaults, overridden through the relevant CLI/environment interfaces."""

    window: int = 30
    allowed: tuple = ()
    found_via: tuple = ("sledgehammer", "try0")
    remediation_template: str = (
        "Fix: run {found_names} on this goal, then write the method it returns. "
        "If nothing is found, the step is too big -- break it into smaller `have`s."
    )
    isabelle_command: str = field(
        default_factory=lambda: os.environ.get("ISABELLE_HOOKS_ISABELLE", "isabelle"))
    identity_timeout_seconds: int = 15
    discovery_timeout_seconds: int = 90


DEFAULTS = GuardDefaults()
