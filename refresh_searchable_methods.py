#!/usr/bin/env python3
"""Prepare the no-guessed-proof method registry outside the write hot path."""
import sys

from isabelle_hooks.config import DEFAULTS
from isabelle_hooks.discovery import refresh_searchable_methods


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else DEFAULTS.isabelle_command
    methods, warning = refresh_searchable_methods(command)
    if warning:
        sys.stderr.write("[isabelle-hooks registry] " + warning + "\n")
    if methods is None:
        return 1
    sys.stdout.write(
        "Prepared searchable-method registry for %s (%d methods).\n"
        % (command, len(methods)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
