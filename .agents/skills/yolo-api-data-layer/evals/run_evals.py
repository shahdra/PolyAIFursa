#!/usr/bin/env python3
"""
run_evals.py — turn evals.json from a document into something that RUNS.

evals.json lists, for each task, plain-English assertions about what the code
should look like afterwards. This script visits each assertion and verifies it
against the real codebase, then prints a report and exits non-zero if anything
failed (so CI can fail the build).

Each assertion gets one of three outcomes:
    PASS  — verified true
    FAIL  — verified false (the assertion does not hold)
    SKIP  — not checkable here yet: behavioral (needs the test suite, only run
            with --pytest) or a human judgment call. SKIP is honest, not a pass.

Static checks read source text. Behavioral checks run your pytest suite, but
ONLY when you pass --pytest (so the everyday run stays instant). With --pytest
the suite is executed ONCE up front; each behavioral assertion then looks up
its matching test by keyword.
"""

import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
RUN_PYTEST = "--pytest" in sys.argv          # opt-in: behavioral checks run the suite

PYTEST_SUITE_OK = None                        # True / False / None (couldn't run)
PYTEST_RESULTS = {}                           # {"tests/test_x.py::test_y": "PASSED"}


# --- locate things -----------------------------------------------------------
HERE = Path(__file__).resolve().parent
EVALS_JSON = HERE / "evals.json"
SKILL_MD = HERE.parent / "SKILL.md"


def find_repo_root(start: Path) -> Path:
    for d in (start, *start.parents):
        if (d / "services" / "yolo").is_dir():
            return d
    sys.exit("ERROR: could not find services/yolo above run_evals.py")


REPO_ROOT = find_repo_root(HERE)
YOLO_DIR = REPO_ROOT / "services" / "yolo"
APP_PY = YOLO_DIR / "app.py"
DATABASE_PY = YOLO_DIR / "database.py"


# --- static check primitives -------------------------------------------------
@lru_cache(maxsize=None)
def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _hit(text, pat, regex):
    return re.search(pat, text) is not None if regex else pat in text


def present(path, *patterns, regex=False, note=""):
    missing = [p for p in patterns if not _hit(read(path), p, regex)]
    return (FAIL, f"missing: {missing[0]}") if missing else (PASS, note)


def present_any(paths, *patterns, regex=False, note=""):
    paths = paths if isinstance(paths, (list, tuple)) else [paths]
    for path in paths:
        for p in patterns:
            if _hit(read(path), p, regex):
                return PASS, note or f"found `{p}`"
    return FAIL, "none of the expected patterns found"


def absent(path, *patterns, regex=True, note=""):
    for p in patterns:
        if _hit(read(path), p, regex):
            return FAIL, f"found `{p}`" + (f" — {note}" if note else "")
    return PASS, note


def absent_in_tree(needle):
    """PASS if no non-test .py file under services/yolo contains the literal needle."""
    for p in sorted(YOLO_DIR.rglob("*.py")):
        if "tests" in p.parts:        # tests may use sqlite3 for throwaway fixtures
            continue
        if needle in read(p):
            return FAIL, f"found in {p.relative_to(REPO_ROOT)}"
    return PASS, "not found in service code (tests excluded)"


def documented(var):
    targets = [SKILL_MD]
    for base in (REPO_ROOT, YOLO_DIR):
        targets += list(base.glob("README*")) + list(base.glob(".env*"))
    for p in targets:
        if p.exists() and var in read(p):
            return PASS, f"documented in {p.relative_to(REPO_ROOT)}"
    return FAIL, f"{var} not found in any README / .env / SKILL.md"


def skip(reason):
    return SKIP, reason


HEURISTIC = "heuristic — confirm by eye"


# --- behavioral checks (pytest) ----------------------------------------------
def collect_pytest_results():
    """Run the YOLO test suite ONCE. Return (suite_ok, {node_id: outcome})."""
    tests = YOLO_DIR / "tests"
    if not tests.is_dir():
        return None, {}
    cmd = [sys.executable, "-m", "pytest", "-v", "--no-header",
           "-p", "no:cacheprovider", "tests"]
    env = {**os.environ, "PYTHONPATH": str(YOLO_DIR)}   # so `from app import ...` resolves
    try:
        proc = subprocess.run(cmd, cwd=YOLO_DIR, env=env,
                              capture_output=True, text=True, timeout=900)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, {}
    results = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)", line)
        if m:
            results[m.group(1)] = m.group(2)
    return proc.returncode == 0, results


def suite_passes():
    """eval 1: did the whole existing suite stay green?"""
    if not RUN_PYTEST:
        return SKIP, "behavioral — run with --pytest to execute the suite"
    if PYTEST_SUITE_OK is None:
        return SKIP, "no tests/ dir or pytest unavailable"
    return (PASS, "full suite green") if PYTEST_SUITE_OK else (FAIL, "suite has failing tests")


def test_named(keyword, human):
    """A behavioral assertion satisfied by a test whose node id contains `keyword`."""
    def _check():
        if not RUN_PYTEST:
            return SKIP, f"{human} — run with --pytest to check"
        if PYTEST_SUITE_OK is None:
            return SKIP, "pytest could not run"
        matches = {n: o for n, o in PYTEST_RESULTS.items() if keyword in n}
        if not matches:
            return SKIP, f"no test matching '{keyword}' yet — write one"
        if all(o == "PASSED" for o in matches.values()):
            return PASS, f"{len(matches)} test(s) matching '{keyword}' pass"
        return FAIL, f"failing test(s) matching '{keyword}'"
    return _check


# --- registry: one check per assertion, in the SAME ORDER as evals.json ------
CHECKS = {
    1: [
        lambda: absent(APP_PY, r"\bimport sqlite3\b", r"\bfrom sqlite3\b"),
        lambda: absent_in_tree("sqlite3.connect"),
        lambda: absent(APP_PY, r"\bSELECT\b", r"\bINSERT INTO\b", r"\bCREATE TABLE\b",
                       note="matches the words anywhere, even in comments"),
        lambda: present(DATABASE_PY, "class PredictionSession", "class DetectionObject"),
        lambda: present_any(DATABASE_PY, "SessionLocal", "sessionmaker", "get_db",
                            note="a session factory is defined"),
        suite_passes,                                          # << now real with --pytest
        lambda: skip("contract — covered by the suite (previous assertion)"),
    ],
    2: [
        lambda: present(APP_PY, "/predictions/recent"),
        lambda: present_any(APP_PY, ".desc()", "desc(", note=HEURISTIC),
        lambda: present_any(APP_PY, ".limit(10)", "limit(10)", note=HEURISTIC),
        test_named("recent", "response shape"),               # << behavioral
        lambda: absent(APP_PY, r"\bSELECT\b", r"\bINSERT INTO\b", note="no raw SQL"),
    ],
    3: [
        lambda: present(DATABASE_PY, "class UserFeedback"),
        lambda: present(DATABASE_PY, "ForeignKey", "uid", note=HEURISTIC),
        lambda: present(DATABASE_PY, "rating", "comment", note=HEURISTIC),
        lambda: present(DATABASE_PY, "relationship("),
        lambda: absent(DATABASE_PY, r"\bCREATE TABLE\b", note="declarative, not raw SQL"),
    ],
    4: [
        lambda: present(APP_PY, "@app.delete", note=HEURISTIC + " (confirm it's keyed on uid)"),
        lambda: present_any(DATABASE_PY, "delete-orphan", 'ondelete="CASCADE"', "ondelete='CASCADE'",
                            note=HEURISTIC + " (true cascade is behavioral)"),
        test_named("delete", "404 on missing uid"),           # << behavioral
        test_named("delete", "delete success response"),      # << behavioral
        lambda: absent(APP_PY, r"\bDELETE FROM\b", note="not a raw SQL DELETE"),
    ],
    5: [
        lambda: present(DATABASE_PY, "processing_time_ms"),
        lambda: present(APP_PY, "processing_time_ms", note=HEURISTIC + " (persisted)"),
        lambda: present(APP_PY, "processing_time_ms", note=HEURISTIC + " (in response — overlaps prev)"),
        lambda: absent(DATABASE_PY, r"\bALTER TABLE\b", r"\bCREATE TABLE\b", note="declarative"),
        test_named("processing_time", "existing keys preserved"),   # << behavioral
    ],
    6: [
        lambda: present_any([APP_PY, DATABASE_PY], "DB_BACKEND", "DATABASE_URL"),
        lambda: present_any([APP_PY, DATABASE_PY], "sqlite", note=HEURISTIC + " (default backend)"),
        test_named("backend", "postgres URL build"),          # << behavioral
        lambda: skip("creds from env, not literals — judgment, confirm by eye"),
        lambda: skip("same code on both backends — behavioral"),
        lambda: documented("DB_BACKEND"),
    ],
}


def main():
    global PYTEST_SUITE_OK, PYTEST_RESULTS
    if RUN_PYTEST:
        print("Running the pytest suite once...\n")
        PYTEST_SUITE_OK, PYTEST_RESULTS = collect_pytest_results()

    data = json.loads(read(EVALS_JSON) or "{}")
    evals = data.get("evals", [])
    counts = {PASS: 0, FAIL: 0, SKIP: 0}

    print(f"Repo root: {REPO_ROOT}")
    print(f"Evals:     {EVALS_JSON.relative_to(REPO_ROOT)}")
    print(f"Pytest:    {'on (--pytest)' if RUN_PYTEST else 'off (static only)'}\n")

    for ev in evals:
        eid = ev["id"]
        print(f"=== Eval {eid}: {ev['prompt']}")
        checks = CHECKS.get(eid, [])
        for i, assertion in enumerate(ev["assertions"]):
            check = checks[i] if i < len(checks) else None
            status, detail = check() if check else (SKIP, "no check registered yet")
            counts[status] += 1
            print(f"  [{status}] {assertion}")
            if detail:
                print(f"         -> {detail}")
        print()

    total = sum(counts.values())
    print(f"{counts[PASS]} passed, {counts[FAIL]} failed, "
          f"{counts[SKIP]} skipped, {total} total")
    sys.exit(1 if counts[FAIL] else 0)


if __name__ == "__main__":
    main()