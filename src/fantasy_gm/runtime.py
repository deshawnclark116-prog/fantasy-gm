"""Runtime/build fingerprint for reproducibility envelopes and model artifacts.

Bit-exact reproducibility is only claimed for a pinned environment (``uv.lock``); the lockfile
hash is recorded so a run can later be matched to the exact dependency set.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np

from fantasy_gm import __version__
from fantasy_gm.domain.artifacts import RuntimeFingerprint

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit or None


def _lockfile_hash() -> str | None:
    lock = Path(os.environ.get("FANTASY_GM_LOCKFILE", _REPO_ROOT / "uv.lock"))
    try:
        return hashlib.sha256(lock.read_bytes()).hexdigest()
    except OSError:
        return None


@lru_cache(maxsize=1)
def runtime_fingerprint() -> RuntimeFingerprint:
    return RuntimeFingerprint(
        python_version=platform.python_version(),
        numpy_version=np.__version__,
        platform=platform.platform(),
        package_version=__version__,
        code_commit=os.environ.get("FANTASY_GM_CODE_COMMIT") or _git_commit(),
        lockfile_hash=_lockfile_hash(),
    )
