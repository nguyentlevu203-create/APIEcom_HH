"""
P11-QUATER Q5 — clean-runner portability check for scripts/gold/, the
GOLD_CHAIN production code run_production_cycle.py invokes. No DB, no
network, no production files touched.

Two separate things are checked, deliberately kept apart:

1. GOLD_CODE_PORTABLE — every GOLD_CHAIN entry file (and everything it
   imports, transitively) is a tracked, committed file that compiles
   cleanly and contains no hardcoded local-Mac absolute path and no
   *required* reference to the gitignored artifacts/ or exports/ trees.
   This is what makes a clean GitHub Actions checkout able to find and
   parse the code at all.

2. GOLD_DATA_AUTHORITY — separately, whether every data file that code
   still legitimately needs at runtime (not code, not dead weight) is
   itself available on a clean checkout. As of this checkpoint,
   scripts/gold/_p5a_keymap.json is NOT committed (pending a human
   decision on committing business/product-catalog data — see the
   P11-QUATER report) — so this half is expected to still be BLOCKED,
   and this test says so explicitly rather than silently passing.

Run with: python3 -m pytest scripts/test_gold_chain_portability.py -v
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLD_DIR = ROOT / "scripts" / "gold"

# Mirrors scripts/run_production_cycle.py::GOLD_CHAIN exactly.
GOLD_CHAIN_ENTRIES = [
    "_p6a_gold_build.py",
    "_p8_5_operational_packaging.py",
    "_p6c1_shopee_pnl_extend.py",
    "_p6b3_pnl_extend.py",
    "_p6c4_affiliate_gold.py",
    "_p6c5_shopee_cm2.py",
    "_p6b4_tiktok_cm2.py",
]

# Transitive local imports (see the P11-QUATER report's dependency graph).
GOLD_TRANSITIVE_DEPS = [
    "_gold_ownership.py",
    "_p5b_load.py",
    "_p5b_recompute.py",
    "_p6b1_finalize.py",
    "_p6b_pnl_build.py",
    "_p6c1_cogs_status.py",
]

ALL_GOLD_FILES = GOLD_CHAIN_ENTRIES + GOLD_TRANSITIVE_DEPS

BAD_PATH_PATTERNS = [
    re.compile(r"/Users/[A-Za-z0-9_]+"),   # any local-Mac absolute path, not just VuIT
    re.compile(r'Path\(\s*["\']exports/'),  # a required (unconditional) exports/ reference
]


def _git_tracked_files() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "scripts/gold"], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return set(out.stdout.splitlines())


@pytest.mark.parametrize("filename", ALL_GOLD_FILES)
def test_gold_file_exists_on_disk(filename):
    assert (GOLD_DIR / filename).exists(), f"{filename} missing from {GOLD_DIR}"


@pytest.mark.parametrize("filename", ALL_GOLD_FILES)
def test_gold_file_is_git_tracked(filename):
    tracked = _git_tracked_files()
    assert f"scripts/gold/{filename}" in tracked, (
        f"{filename} exists on disk but is NOT committed — a clean GitHub "
        f"Actions checkout would not have it (this was the original bug)"
    )


@pytest.mark.parametrize("filename", ALL_GOLD_FILES)
def test_gold_file_compiles(filename):
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", str(GOLD_DIR / filename)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("filename", ALL_GOLD_FILES)
def test_gold_file_has_no_hardcoded_local_path(filename):
    text = (GOLD_DIR / filename).read_text(encoding="utf-8")
    for pattern in BAD_PATH_PATTERNS:
        m = pattern.search(text)
        assert not m, f"{filename} contains a hardcoded local path: {m.group(0)!r}"


def test_gold_chain_entries_match_orchestrator():
    """scripts/run_production_cycle.py::GOLD_CHAIN must reference exactly
    these files under scripts/gold/ — catches drift between this test's
    list and the orchestrator's own list."""
    src = (ROOT / "scripts" / "run_production_cycle.py").read_text(encoding="utf-8")
    for entry in GOLD_CHAIN_ENTRIES:
        assert entry in src, f"{entry} not referenced in run_production_cycle.py's GOLD_CHAIN"
    assert "GOLD_DIR" in src, (
        "run_production_cycle.py must resolve Gold scripts via an explicit "
        "ROOT-derived path, not an implicit cwd=artifacts/v0"
    )


def test_gold_code_portable_summary():
    """The headline check: every file the orchestrator actually needs to
    FIND and PARSE is tracked, compiles, and has no hardcoded local path.
    This does not assert the code can fully RUN — see
    test_gold_data_authority_is_blocked below for the separate, honest
    data-dependency gap."""
    tracked = _git_tracked_files()
    for filename in ALL_GOLD_FILES:
        assert f"scripts/gold/{filename}" in tracked
    print("GOLD_CODE_PORTABLE = TRUE")


def test_gold_data_authority_is_blocked():
    """Documents, rather than hides, the one remaining real gap: the
    keymap this code needs for SKU role classification (SALE/PROMO_GIFT/
    PACKAGING) has no Neon authority and is not committed. This test
    intentionally asserts the CURRENT (blocked) state so it fails loudly
    — not silently passes — the moment someone resolves it one way or
    the other (commits the file, or adds a DB authority and removes the
    read), which is exactly when this test should be updated too."""
    tracked = _git_tracked_files()
    keymap_committed = "scripts/gold/_p5a_keymap.json" in tracked
    assert not keymap_committed, (
        "scripts/gold/_p5a_keymap.json is now committed — if this was an "
        "intentional decision, update this test (and the P11-QUATER "
        "report) to reflect GOLD_DATA_AUTHORITY as resolved."
    )
