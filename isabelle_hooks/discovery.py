"""Discovery and persistent caching of Isabelle search-reconstructable methods."""
import hashlib
import json
import os
import subprocess
import tempfile
import time
import shutil

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


def _fast_identity(command):
    """Cheap launcher identity for the PreToolUse hot path (no Isabelle process)."""
    configured = os.environ.get("ISABELLE_HOOKS_IDENTITY")
    executable = shutil.which(command) if not os.path.isabs(command) else command
    executable = os.path.realpath(executable) if executable else command
    try:
        stat = os.stat(executable)
        launcher = (executable, stat.st_size, stat.st_mtime_ns)
    except Exception:
        launcher = (executable, None, None)
    material = (QUERY_VERSION, command, configured, launcher, _file_hash(QUERY_THEORY))
    return hashlib.sha256(repr(material).encode()).hexdigest()[:24]


def _active_cache_path(command):
    command_key = hashlib.sha256(command.encode()).hexdigest()[:20]
    return os.path.join(_cache_root(), "active-" + command_key + ".json")


def _write_active_cache(command, home, fingerprint, methods):
    root = _cache_root()
    os.makedirs(root, exist_ok=True)
    target = _active_cache_path(command)
    fd, tmp = tempfile.mkstemp(prefix="active-", suffix=".json", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({
                "version": QUERY_VERSION,
                "created": time.time(),
                "command": command,
                "home": home,
                "discovery_fingerprint": fingerprint,
                "fast_identity": _fast_identity(command),
                "methods": sorted(methods),
            }, f)
            f.write("\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_searchable_methods(command):
    """Load the prepared registry without launching Isabelle.

    Missing/stale registries return ``None`` so callers can use the conservative
    require-evidence-for-all fallback.  ``refresh_searchable_methods`` is the explicit
    slow lifecycle operation that prepares this manifest.
    """
    path = _active_cache_path(command)
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        if (obj.get("version") != QUERY_VERSION
                or obj.get("command") != command
                or obj.get("fast_identity") != _fast_identity(command)):
            return None, "searchable-method registry is stale; refresh it outside the hook"
        methods = obj.get("methods")
        if (not isinstance(methods, list) or not methods
                or not all(isinstance(method, str) for method in methods)):
            raise ValueError("invalid method registry")
        return set(methods), None
    except FileNotFoundError:
        return None, "searchable-method registry is not prepared; refresh it outside the hook"
    except Exception as exc:
        return None, "searchable-method registry is unavailable: %s" % exc


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
        home, stable, fingerprint = _discover_identity(command)
    except Exception as exc:
        return None, "method discovery failed: %s" % exc

    root = _cache_root()
    current = os.path.join(root, "%s-%s.json" % (stable, fingerprint))
    cached = _load_cache(current)
    if cached:
        try:
            _write_active_cache(command, home, fingerprint, cached)
        except Exception:
            pass
        return cached, None

    lock_file = None
    try:
        os.makedirs(root, exist_ok=True)
        lock_file = open(os.path.join(root, stable + ".lock"), "a+")
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        cached = _load_cache(current)
        if cached:
            try:
                _write_active_cache(command, home, fingerprint, cached)
            except Exception:
                pass
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
        try:
            _write_active_cache(command, home, fingerprint, methods)
        except Exception:
            pass
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
                try:
                    _write_active_cache(command, home, fingerprint, cached)
                except Exception:
                    pass
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


def refresh_searchable_methods(command):
    """Explicit slow registry refresh used at install/update time."""
    return discover_searchable_methods(command)
