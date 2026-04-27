# backtest/conviction_scorer.py
# ═══════════════════════════════════════════════════════════════════
# Compatibility shim: backtests now use the LIVE conviction_scorer.py.
#
# Why this exists:
#   The old backtest/conviction_scorer.py was a stale copy of the live scorer.
#   Because Python puts the script directory on sys.path first, running files
#   from backtest/ could silently import the stale copy instead of the live
#   module. This shim keeps old imports working while forcing one source of
#   truth: ../conviction_scorer.py.
#
# Do not add scorer logic here. Update the repo-root conviction_scorer.py.
# ═══════════════════════════════════════════════════════════════════

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_live_scorer() -> ModuleType:
    repo_root = Path(os.environ.get("BOT_REPO_PATH") or Path(__file__).resolve().parents[1]).resolve()
    live_path = repo_root / "conviction_scorer.py"
    if not live_path.exists():
        raise ImportError(f"Live conviction_scorer.py not found at {live_path}")
    if live_path.resolve() == Path(__file__).resolve():
        raise ImportError("backtest conviction_scorer shim resolved to itself; check BOT_REPO_PATH")

    spec = importlib.util.spec_from_file_location("_live_conviction_scorer", str(live_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load live conviction scorer from {live_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_live = _load_live_scorer()

# Explicit public API used by app/backtests.
score_signal = _live.score_signal
ConvictionResult = _live.ConvictionResult

# Common constants/helpers used by validation scripts. Keep these aliases broad
# so older backtest scripts do not need edits.
for _name in dir(_live):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_live, _name))


def __getattr__(name: str) -> Any:
    return getattr(_live, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_live)))


__all__ = [n for n in dir(_live) if not n.startswith("__")]
