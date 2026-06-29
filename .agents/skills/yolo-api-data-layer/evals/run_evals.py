#!/usr/bin/env python3
"""
run_evals.py — turn evals.json from a document into something that RUNS.

evals.json lists, for each task, a set of plain-English assertions about what
the code should look like afterwards. This script visits each assertion and
verifies it against the real codebase, then prints a report and exits non-zero
if anything failed (so CI can fail the build).

Each assertion gets one of three outcomes:
    PASS  — verified true against the source
    FAIL  — verified false (the assertion does not hold)
    SKIP  — no automated check exists yet: it's behavioral (needs the running
            test suite) or a human judgment call. SKIP is honest, not a pass.

The assertion text in evals.json is the human-readable LABEL. The actual
verification logic lives here, mapped to each eval id by position. If you add
an assertion to evals.json without adding a check here, it shows up as SKIP —
on purpose, so missing automation stays visible.
"""

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# --- locate things -----------------------------------------------------------
HERE = Path(__file__).resolve().parent          # .../yolo-api-data-layer/evals
EVALS_JSON = HERE / "evals.json"
SKILL_MD = HERE.parent / "SKILL.md"


def find_repo_root(start: Path) -> Path:
    """Walk upward until we find the folder that holds services/yolo."""
    for d in (start, *start.parents):
        if (d / "services" / "yolo").is_dir():
            return d
    sys.exit("ERROR: could not find services/yolo above run_evals.py")


REPO_ROOT = find_repo_root(HERE)
YOLO_DIR = REPO_ROOT / "services" / "yolo"
APP_PY = YOLO_DIR / "app.py"
DATABASE_PY = YOLO_DIR / "database.py"


# --- tiny check primitives ---------------------------------------------------
@lru_cache(maxsize=None)
def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _hit(text, pat, regex):
    return re.search(pat, text) is not None if regex else pat in text


def present(path, *patterns, regex=False, note=""):
    """PASS if ALL patterns appear in the file (a 'must contain' assertion)."""
    missing = [p for p in patterns if not _hit(read(path), p, regex)]
    if missing:
        return FAIL, f"missing: {missing[0]}"
    return PASS, note


def present_any(paths, *patterns, regex=False, note=""):
    """PASS if ANY pattern appears in ANY of the files."""
    paths = paths if isinstance(paths, (list, tuple)) else [paths]
    for path in paths:
        for p in patterns:
            if _hit(read(path), p, regex):
                return PASS, note or f"found `{p}`"
    return FAIL, "none of the expected patterns found"


def absent(path, *patterns, regex=True, note=""):
    """PASS if NONE of the patterns appear (a 'must NOT contain' assertion)."""
    for p in patterns:
        if _hit(read(path), p, regex):
            return FAIL, f"found `{p}`" + (f" — {note}" if note else "")
    return PASS, note


def absent_in_tree(needle):
    """PASS if no non-test .py file under services/yolo contains the literal needle."""
    for p in sorted(YOLO_DIR.rglob("*.py")):
        if "tests" in p.parts:        # tests may use sqlite3 for throwaway fixtures — that's fine
            continue
        if needle in read(p):
            return FAIL, f"found in {p.relative_to(REPO_ROOT)}"
    return PASS, "not found in service code (tests excluded)"

def documented(var):
    """PASS if the env var is mentioned in a README, a .env file, or the skill."""
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


# --- the registry: one check per assertion, in the SAME ORDER as evals.json --
# A lambda lets the file reads happen at run time, not import time.
CHECKS = {
    # 1. Refactor to SQLAlchemy
    1: [
        lambda: absent(APP_PY, r"\bimport sqlite3\b", r"\bfrom sqlite3\b"),
        lambda: absent_in_tree("sqlite3.connect"),
        lambda: absent(APP_PY, r"\bSELECT\b", r"\bINSERT INTO\b", r"\bCREATE TABLE\b",
                       note="matches the words anywhere, even in comments"),
        lambda: present(DATABASE_PY, "class PredictionSession", "class DetectionObject"),
        lambda: present_any(DATABASE_PY, "SessionLocal", "sessionmaker", "get_db",
                            note="a session factory is defined"),
        lambda: skip("behavioral — the pytest suite (wired in step 2)"),
        lambda: skip("contract — covered by the API tests, not a static fact"),
    ],
    # 2. GET /predictions/recent
    2: [
        lambda: present(APP_PY, "/predictions/recent"),
        lambda: present_any(APP_PY, ".desc()", "desc(", note=HEURISTIC),
        lambda: present_any(APP_PY, ".limit(10)", "limit(10)", note=HEURISTIC),
        lambda: skip("response shape — behavioral/judgment, defer to a test"),
        lambda: absent(APP_PY, r"\bSELECT\b", r"\bINSERT INTO\b", note="no raw SQL"),
    ],
    # 3. UserFeedback table
    3: [
        lambda: present(DATABASE_PY, "class UserFeedback"),
        lambda: present(DATABASE_PY, "ForeignKey", "uid", note=HEURISTIC),
        lambda: present(DATABASE_PY, "rating", "comment", note=HEURISTIC),
        lambda: present(DATABASE_PY, "relationship("),
        lambda: absent(DATABASE_PY, r"\bCREATE TABLE\b", note="declarative, not raw SQL"),
    ],
    # 4. Delete a session + its detection objects
    4: [
        lambda: present(APP_PY, "@app.delete", note=HEURISTIC + " (confirm it's keyed on uid)"),
        lambda: present_any(DATABASE_PY, "delete-orphan", 'ondelete="CASCADE"', "ondelete='CASCADE'",
                            note=HEURISTIC + " (true cascade is behavioral)"),
        lambda: skip("404 on missing uid — behavioral, defer to a test"),
        lambda: skip("success response — behavioral, defer to a test"),
        lambda: absent(APP_PY, r"\bDELETE FROM\b", note="not a raw SQL DELETE"),
    ],
    # 5. processing_time_ms column
    5: [
        lambda: present(DATABASE_PY, "processing_time_ms"),
        lambda: present(APP_PY, "processing_time_ms", note=HEURISTIC + " (persisted)"),
        lambda: present(APP_PY, "processing_time_ms", note=HEURISTIC + " (in response — overlaps prev)"),
        lambda: absent(DATABASE_PY, r"\bALTER TABLE\b", r"\bCREATE TABLE\b", note="declarative"),
        lambda: skip("existing keys preserved — behavioral, defer to a test"),
    ],
    # 6. Configurable database backend
    6: [
        lambda: present_any([APP_PY, DATABASE_PY], "DB_BACKEND", "DATABASE_URL"),
        lambda: present_any([APP_PY, DATABASE_PY], "sqlite", note=HEURISTIC + " (default backend)"),
        lambda: skip("postgres URL build — behavioral (import with env set)"),
        lambda: skip("creds from env, not literals — judgment, confirm by eye"),
        lambda: skip("same code on both backends — behavioral"),
        lambda: documented("DB_BACKEND"),
    ],
}


def main():
    data = json.loads(read(EVALS_JSON) or "{}")
    evals = data.get("evals", [])
    counts = {PASS: 0, FAIL: 0, SKIP: 0}

    print(f"Repo root: {REPO_ROOT}")
    print(f"Evals:     {EVALS_JSON.relative_to(REPO_ROOT)}\n")

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
