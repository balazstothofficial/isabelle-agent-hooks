"""Shared subprocess and payload fixtures for hook contract tests."""
import json
import os
import shutil
import subprocess
import sys
import tempfile


def run_hook(script, payload, args=()):
    proc = subprocess.run(
        [sys.executable, script, *args],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    return proc.returncode, proc.stderr


def thy_write(content):
    return {"tool_name": "Write", "tool_input": {
        "file_path": "Foo.thy", "content": content}}


def run_without_package(script, payload):
    """Run an entry point in isolation, without its shared package installed."""
    with tempfile.TemporaryDirectory() as directory:
        isolated = os.path.join(directory, os.path.basename(script))
        shutil.copy2(script, isolated)
        return run_hook(isolated, payload)
