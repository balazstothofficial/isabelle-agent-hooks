"""Discovery and persistent caching of Isabelle search-reconstructable methods."""
import hashlib
import json
import os
import subprocess
import tempfile
import time

try:
    import fcntl
except ImportError:  # non-POSIX manual use: cache remains atomic, just not locked
    fcntl = None

from .methods import _IDENT
from .config import DEFAULTS


QUERY_MARKER = "NO_GUESSED_PROOFS_METHOD "
QUERY_VERSION = "1"
QUERY_THEORY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "Hook_Searchable_Methods.thy")
DISCOVERY_SOURCE_PATHS = (
    "src/HOL/Tools/try0.ML",
    "src/HOL/Try0_HOL.thy",
    "src/HOL/Tools/Sledgehammer/sledgehammer_prover.ML",
    "src/HOL/Tools/Sledgehammer/sledgehammer_proof_methods.ML",
)


def _cache_root():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "isabelle-agent-hooks", "searchable-methods")


def _file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return "missing"


def _load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        methods = obj.get("methods") if isinstance(obj, dict) else None
        if isinstance(methods, list) and methods and all(isinstance(m, str) for m in methods):
            return set(methods)
    except Exception:
        pass
    return None


def _discover_identity(command):
    proc = subprocess.run([command, "getenv", "-b", "ISABELLE_HOME"], text=True,
                          capture_output=True, timeout=DEFAULTS.identity_timeout_seconds)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError("could not resolve ISABELLE_HOME")
    home = os.path.realpath(proc.stdout.strip().splitlines()[-1])
    stable = hashlib.sha256((command + "\0" + home).encode()).hexdigest()[:20]
    material = [QUERY_VERSION, command, home, _file_hash(QUERY_THEORY)]
    material.extend(_file_hash(os.path.join(home, rel)) for rel in DISCOVERY_SOURCE_PATHS)
    fingerprint = hashlib.sha256("\0".join(material).encode()).hexdigest()[:24]
    return home, stable, fingerprint


def _run_discovery(command):
    proc = subprocess.run(
        [command, "process_theories", "-O", "-l", "HOL", "-D",
         os.path.dirname(QUERY_THEORY), "Hook_Searchable_Methods"],
        text=True, capture_output=True, timeout=DEFAULTS.discovery_timeout_seconds)
    output = proc.stdout + "\n" + proc.stderr
    methods = set()
    for line in output.splitlines():
        marker = line.find(QUERY_MARKER)
        if marker < 0:
            continue
        expression = line[marker + len(QUERY_MARKER):].strip()
        # Parenthesised reconstructors are printed as `(metis ...)` / `(smt ...)`.
        match = _IDENT.search(expression)
        if match:
            methods.add(match.group(0))
    if proc.returncode != 0 or not methods:
        detail = next((line.strip() for line in output.splitlines() if line.strip()),
                      "no method registry was produced")
        raise RuntimeError(detail)
    return methods


def discover_searchable_methods(command):
    """Return (methods, warning). A stale valid cache is preferred to unsafe emptiness."""
    try:
        _home, stable, fingerprint = _discover_identity(command)
    except Exception as exc:
        return None, "method discovery failed: %s" % exc

    root = _cache_root()
    current = os.path.join(root, "%s-%s.json" % (stable, fingerprint))
    cached = _load_cache(current)
    if cached:
        return cached, None

    lock_file = None
    try:
        os.makedirs(root, exist_ok=True)
        lock_file = open(os.path.join(root, stable + ".lock"), "a+")
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        cached = _load_cache(current)
        if cached:
            return cached, None
        methods = _run_discovery(command)
        fd, tmp = tempfile.mkstemp(prefix="methods-", suffix=".json", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": QUERY_VERSION, "created": time.time(),
                           "methods": sorted(methods)}, f)
                f.write("\n")
            os.replace(tmp, current)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return methods, None
    except Exception as exc:
        stale = []
        try:
            stale = sorted(
                (p for p in os.listdir(root) if p.startswith(stable + "-") and p.endswith(".json")),
                key=lambda p: os.path.getmtime(os.path.join(root, p)), reverse=True)
        except Exception:
            pass
        for name in stale:
            cached = _load_cache(os.path.join(root, name))
            if cached:
                return cached, "method discovery failed; using previous cache: %s" % exc
        return None, "method discovery failed and no cache is available: %s" % exc
    finally:
        if lock_file is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            lock_file.close()
