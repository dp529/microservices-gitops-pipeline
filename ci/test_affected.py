#!/usr/bin/env python
"""Tests for the affected-service detector.

Cases 1-9 and 12 drive the detector with explicit path lists. Cases 10 and 11
use a real throwaway git repository so that deletions, renames and multi-commit
ranges are exercised against actual `git diff` output rather than a simulation
of it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import affected  # noqa: E402

SERVICES, FANOUT, IGNORE = affected.load_config()
ALL = [s["name"] for s in SERVICES]

results = []


def run(files):
    a, reasons, unknown, fan = affected.classify(files, SERVICES, FANOUT, IGNORE)
    return affected.build_matrix(a, SERVICES), unknown


def check(num, title, files, expect_names, expect_unknown=False):
    matrix, unknown = run(files)
    got = [m["name"] for m in matrix]
    ok = sorted(got) == sorted(expect_names) and bool(unknown) == expect_unknown
    results.append((ok, num, title, len(got), len(expect_names)))
    print("%-5s %2d. %-46s -> %d service(s)%s"
          % ("PASS" if ok else "FAIL", num, title, len(got),
             "  UNKNOWN REPORTED" if unknown else ""))
    if not ok:
        print("         expected %s" % sorted(expect_names))
        print("         got      %s  unknown=%s" % (sorted(got), unknown))
    return matrix


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, check=True).stdout


def make_repo():
    """A throwaway repo carrying just enough of the real layout."""
    tmp = Path(tempfile.mkdtemp(prefix="affected-test-"))
    git(tmp.parent, "init", "-q", str(tmp)) if False else None
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True)
    git(tmp, "config", "user.email", "test@example.invalid")
    git(tmp, "config", "user.name", "test")
    for s in SERVICES:
        d = tmp / s["watch"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "placeholder.txt").write_text("x\n", encoding="utf-8")
    (tmp / "protos").mkdir(parents=True, exist_ok=True)
    (tmp / "protos" / "demo.proto").write_text("syntax = \"proto3\";\n",
                                               encoding="utf-8")
    (tmp / "docs").mkdir(parents=True, exist_ok=True)
    (tmp / "docs" / "notes.md").write_text("x\n", encoding="utf-8")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-q", "-m", "base")
    return tmp


def check_git(num, title, repo, base, head, expect_names):
    files = affected.changed_files(base, head, None)
    cwd = os.getcwd()
    try:
        os.chdir(repo)
        files = affected.changed_files(base, head, None)
    finally:
        os.chdir(cwd)
    matrix, unknown = run(files)
    got = [m["name"] for m in matrix]
    ok = sorted(got) == sorted(expect_names)
    results.append((ok, num, title, len(got), len(expect_names)))
    print("%-5s %2d. %-46s -> %d service(s)"
          % ("PASS" if ok else "FAIL", num, title, len(got)))
    if not ok:
        print("         expected %s" % sorted(expect_names))
        print("         got      %s" % sorted(got))
        print("         files    %s" % files)


def main():
    print("Affected-service detector - test cases\n")

    # ---- 1 ----------------------------------------------------------
    m = check(1, "paymentservice only",
              ["src/paymentservice/charge.js"], ["paymentservice"])
    assert m[0]["context"] == "src/paymentservice"

    # ---- 2 ----------------------------------------------------------
    m = check(2, "cartservice only (watch != build context)",
              ["src/cartservice/tests/CartServiceTests.cs"], ["cartservice"])
    ctx = m[0]["context"]
    ok = ctx == "src/cartservice/src"
    results.append((ok, 2.1, "cartservice build context is src/cartservice/src", 1, 1))
    print("%-5s 2a. %-46s -> %s"
          % ("PASS" if ok else "FAIL", "context resolves to src/cartservice/src", ctx))

    # a change inside the build context still resolves the same way
    m = check(3, "cartservice change inside the build context",
              ["src/cartservice/src/Program.cs"], ["cartservice"])
    assert m[0]["context"] == "src/cartservice/src"

    # ---- 4 ----------------------------------------------------------
    check(4, "two service changes",
          ["src/paymentservice/charge.js", "src/shippingservice/quote.go"],
          ["paymentservice", "shippingservice"])

    # ---- 5 ----------------------------------------------------------
    check(5, "protos/demo.proto fans out to all ten",
          ["protos/demo.proto"], ALL)

    # ---- 6 ----------------------------------------------------------
    check(6, "proto plus one service still yields all ten",
          ["protos/demo.proto", "src/paymentservice/charge.js"], ALL)

    # ---- 7 ----------------------------------------------------------
    check(7, "docs only", ["docs/deployable-units.md"], [])
    check(8, "gitops only",
          ["gitops/overlays/prod/frontend/kustomization.yaml",
           "gitops/base/frontend/deployment.yaml"], [])
    check(9, "README and repo metadata only",
          ["README.md", "SERVICES.md", ".gitignore", ".gitattributes"], [])

    # ---- unknown ----------------------------------------------------
    check(10, "unknown directory under src/ is reported",
          ["src/random-service/main.go"], [], expect_unknown=True)
    check(11, "unknown alongside a real change still reports",
          ["src/paymentservice/charge.js", "src/random-service/main.go"],
          ["paymentservice"], expect_unknown=True)

    # a near-miss name must not match by prefix
    check(12, "src/cart is not src/cartservice",
          ["src/cart/thing.go"], [], expect_unknown=True)

    # ---- 12 empty ---------------------------------------------------
    check(13, "empty diff", [], [])

    # ---- real git: deletions, renames, multi-commit ------------------
    repo = make_repo()
    try:
        # deletion
        (repo / "src/paymentservice/placeholder.txt").unlink()
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "delete a paymentservice file")
        check_git(14, "deleted file still selects its service",
                  repo, "HEAD~1", "HEAD", ["paymentservice"])

        # rename within a service
        git(repo, "mv", "src/frontend/placeholder.txt",
            "src/frontend/renamed.txt")
        git(repo, "commit", "-q", "-m", "rename inside frontend")
        check_git(15, "renamed file still selects its service",
                  repo, "HEAD~1", "HEAD", ["frontend"])

        # rename ACROSS services - both must be selected
        git(repo, "mv", "src/adservice/placeholder.txt",
            "src/emailservice/moved.txt")
        git(repo, "commit", "-q", "-m", "move a file between services")
        check_git(16, "cross-service rename selects both",
                  repo, "HEAD~1", "HEAD", ["adservice", "emailservice"])

        # multi-commit union
        (repo / "src/currencyservice/a.txt").write_text("1\n", encoding="utf-8")
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "c1")
        (repo / "src/checkoutservice/b.txt").write_text("1\n", encoding="utf-8")
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "c2")
        (repo / "docs/notes.md").write_text("2\n", encoding="utf-8")
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "c3 docs only")
        check_git(17, "union across three commits, docs ignored",
                  repo, "HEAD~3", "HEAD",
                  ["currencyservice", "checkoutservice"])

        # multi-commit where one commit touches protos
        (repo / "src/frontend/c.txt").write_text("1\n", encoding="utf-8")
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "c4")
        (repo / "protos/demo.proto").write_text("syntax = \"proto3\";\n// x\n",
                                                encoding="utf-8")
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "c5 proto")
        check_git(18, "proto touched in any commit fans out",
                  repo, "HEAD~2", "HEAD", ALL)
    finally:
        shutil.rmtree(repo, ignore_errors=True)

    # ---- exit code for unknown --------------------------------------
    rc = affected.main(["--files", "src/random-service/main.go", "--json"])
    ok = rc == 2
    results.append((ok, 19, "unknown path exits non-zero", 0, 0))
    print("%-5s 19. %-46s -> exit %d" % ("PASS" if ok else "FAIL",
                                          "unknown path exits 2", rc))

    rc = affected.main(["--files", "docs/x.md", "--json"])
    ok = rc == 0
    results.append((ok, 20, "clean run exits zero", 0, 0))
    print("%-5s 20. %-46s -> exit %d" % ("PASS" if ok else "FAIL",
                                          "ignored-only path exits 0", rc))

    # ---- config integrity -------------------------------------------
    print()
    repo_root = Path(__file__).resolve().parents[1]
    bad = 0
    for s in SERVICES:
        if not (repo_root / s["dockerfile"]).exists():
            print("FAIL      %s dockerfile missing: %s" % (s["name"], s["dockerfile"]))
            bad += 1
        if not (repo_root / s["context"]).is_dir():
            print("FAIL      %s context missing: %s" % (s["name"], s["context"]))
            bad += 1
        if not (repo_root / s["watch"]).is_dir():
            print("FAIL      %s watch path missing: %s" % (s["name"], s["watch"]))
            bad += 1
    results.append((bad == 0, 21, "every configured path exists on disk", 0, 0))
    print("%-5s 21. %-46s -> %d service(s)"
          % ("PASS" if bad == 0 else "FAIL",
             "services.yaml paths all exist on disk", len(SERVICES)))

    # every service directory on disk must be configured
    on_disk = {d.name for d in (repo_root / "src").iterdir() if d.is_dir()}
    configured = {s["name"] for s in SERVICES}
    missing = on_disk - configured
    results.append((not missing, 22, "no unconfigured service directories", 0, 0))
    print("%-5s 22. %-46s -> %s"
          % ("PASS" if not missing else "FAIL",
             "every src/ directory is configured",
             "none missing" if not missing else sorted(missing)))

    print()
    passed = sum(1 for r in results if r[0])
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
