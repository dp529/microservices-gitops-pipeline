#!/usr/bin/env python
"""Decide which services a set of changed files requires rebuilding.

Reads changed paths (from `git diff --name-only`, or from --files), maps them
against ci/services.yaml, and emits a JSON matrix for GitHub Actions.

    python ci/affected.py --base origin/main --head HEAD
    python ci/affected.py --files src/paymentservice/main.go

Exit codes:
    0  decision made (the matrix may legitimately be empty)
    2  a path under src/ belongs to no known service

Nothing is built here. This answers only: which services would CI build?
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent / "services.yaml"
SRC_ROOT = "src"


def load_config(path=CONFIG):
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = cfg["services"]
    names = [s["name"] for s in services]
    if len(names) != len(set(names)):
        raise SystemExit("services.yaml contains duplicate service names")
    return services, cfg.get("fanout", []), cfg.get("ignore", [])


def normalize(path):
    """Git reports forward slashes; be tolerant of Windows input anyway."""
    return path.replace("\\", "/").strip()


def under(path, prefix):
    """True if `path` is `prefix` itself or lives beneath it.

    Compared segment-wise so that `src/cart` does not match
    `src/cartservice/...`.
    """
    if path == prefix:
        return True
    return path.startswith(prefix.rstrip("/") + "/")


def changed_files(base, head, extra_files):
    if extra_files:
        return [normalize(f) for f in extra_files if f.strip()]
    # --diff-filter is deliberately NOT restricted: a deleted or renamed file
    # is still a change to that service. Renames are reported as two paths
    # with --no-renames, so both the old and new service are picked up.
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "%s...%s" % (base, head)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # A bad or unreachable ref must not look like "nothing changed".
        # Failing loudly here is the difference between CI skipping every
        # build and CI telling you the range was wrong.
        raise SystemExit(
            "git diff %s...%s failed (exit %d):\n%s\n"
            "Check that both refs exist and that the clone has enough "
            "history (actions/checkout needs fetch-depth: 0)."
            % (base, head, proc.returncode, proc.stderr.strip())
        )
    return [normalize(l) for l in proc.stdout.splitlines() if l.strip()]


def classify(files, services, fanout, ignore):
    """Return (affected_names, reasons, unknown_paths)."""
    affected = set()
    reasons = {}
    unknown = []
    fanout_hits = []

    for f in files:
        # 1. fan-out paths affect everything
        if any(under(f, p) for p in fanout):
            fanout_hits.append(f)
            continue

        # 2. explicitly ignored paths affect nothing
        if any(under(f, p) for p in ignore):
            continue

        # 3. a known service's watch path
        match = next((s for s in services if under(f, s["watch"])), None)
        if match:
            affected.add(match["name"])
            reasons.setdefault(match["name"], []).append(f)
            continue

        # 4. anything else under src/ is an unknown deployable unit
        if under(f, SRC_ROOT):
            unknown.append(f)
            continue

        # 5. anything else at the repository root is not an application file
        #    and does not trigger a build.

    if fanout_hits:
        for s in services:
            affected.add(s["name"])
            reasons.setdefault(s["name"], []).extend(fanout_hits)

    return affected, reasons, unknown, bool(fanout_hits)


def build_matrix(affected, services):
    """Emit entries in services.yaml order, so output is deterministic."""
    return [
        {
            "name": s["name"],
            "context": s["context"],
            "dockerfile": s["dockerfile"],
        }
        for s in services
        if s["name"] in affected
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main",
                    help="base ref for the diff (default: origin/main)")
    ap.add_argument("--head", default="HEAD",
                    help="head ref for the diff (default: HEAD)")
    ap.add_argument("--files", nargs="*",
                    help="use these paths instead of running git diff")
    ap.add_argument("--json", action="store_true",
                    help="print only the matrix JSON")
    ap.add_argument("--github-output", metavar="FILE",
                    help="append matrix= and any= to a GitHub Actions output file")
    args = ap.parse_args(argv)

    services, fanout, ignore = load_config()
    files = changed_files(args.base, args.head, args.files)
    affected, reasons, unknown, was_fanout = classify(files, services, fanout, ignore)
    matrix = build_matrix(affected, services)
    payload = {"services": matrix}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("Changed files: %d" % len(files))
        for f in files:
            print("    %s" % f)
        print()
        if was_fanout:
            print("Shared contract changed under %s - fanning out to all services."
                  % ", ".join(fanout))
            print()
        print("Services to build: %d" % len(matrix))
        for m in matrix:
            why = reasons.get(m["name"], [])
            print("    %-24s context=%s" % (m["name"], m["context"]))
            if m["context"] != next(s["watch"] for s in services
                                    if s["name"] == m["name"]):
                print("        note: build context differs from watch path (%s)"
                      % next(s["watch"] for s in services if s["name"] == m["name"]))
            for w in why[:3]:
                print("        <- %s" % w)
            if len(why) > 3:
                print("        <- ... and %d more" % (len(why) - 3))
        print()
        print(json.dumps(payload, indent=2))

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write("matrix=%s\n" % json.dumps(payload))
            fh.write("any=%s\n" % ("true" if matrix else "false"))

    if unknown:
        sys.stderr.write(
            "\nERROR: unknown deployable unit.\n"
            "These paths are under %s/ but belong to no service in "
            "ci/services.yaml:\n" % SRC_ROOT)
        for u in sorted(set(unknown)):
            sys.stderr.write("    %s\n" % u)
        sys.stderr.write(
            "\nAdd the service to ci/services.yaml (name, watch, context, "
            "dockerfile)\nor remove the path. Refusing to guess.\n")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
