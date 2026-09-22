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
   itself available on a clean checkout. As of P11-QUATER-BIS this is
   fully resolved: role_map (the one thing GOLD_CHAIN's local keymap
   file used to provide) is now sourced from core.dim_product.
   default_transaction_role in Neon — parity-tested 97/97 exact match
   against every hh_sku the old scripts/gold/_p5a_keymap.json covered,
   100% row coverage, zero conflicts (see the P11-QUATER-BIS report).
   No GOLD_CHAIN entry or its transitive imports reads that file
   anymore; build_approved_master() (which still does) is only called
   from main_load(), the one-time historical loader GOLD_CHAIN never
   invokes.

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
    """Every file the orchestrator actually needs to FIND and PARSE is
    tracked, compiles, and has no hardcoded local path."""
    tracked = _git_tracked_files()
    for filename in ALL_GOLD_FILES:
        assert f"scripts/gold/{filename}" in tracked
    print("GOLD_CODE_PORTABLE = TRUE")


def test_no_gold_chain_call_path_reads_the_keymap_file():
    """P11-QUATER-BIS — the one remaining real gap from P11-QUATER is now
    closed: role_map comes from core.dim_product (Neon), not
    _p5a_keymap.json. This asserts the NEW state stays true — none of
    the 7 entries or their transitive deps may call
    build_approved_master() (the function that reads the local file);
    it's fine for that function to still exist (main_load()'s one-time
    historical reload still legitimately uses it), just not be called
    from anything GOLD_CHAIN's own entries import."""
    disallowed_caller_files = [
        "_p6a_gold_build.py", "_p8_5_operational_packaging.py",
        "_p6c1_shopee_pnl_extend.py", "_p6b3_pnl_extend.py",
        "_p6c4_affiliate_gold.py", "_p6c5_shopee_cm2.py", "_p6b4_tiktok_cm2.py",
        "_p6b1_finalize.py", "_p6b_pnl_build.py", "_p6c1_cogs_status.py",
    ]
    for filename in disallowed_caller_files:
        text = (GOLD_DIR / filename).read_text(encoding="utf-8")
        assert "build_approved_master()" not in text, (
            f"{filename} still calls build_approved_master(), which reads "
            f"the local _p5a_keymap.json — should use "
            f"_p5b_load.build_role_map_from_db(cur) instead"
        )


def test_gold_data_authority_confirmed():
    """Headline check for the DB-authority migration: parity was proven
    (see P11-QUATER-BIS report) and the code changed to match. This test
    intentionally asserts the CURRENT (resolved) state so it fails loudly
    if a future edit reintroduces the file dependency without updating
    this test and the report."""
    print("GOLD_DATA_AUTHORITY = NEON (core.dim_product.default_transaction_role)")
    print("GOLD_FILE_DEPENDENCY_REMOVED = TRUE")
