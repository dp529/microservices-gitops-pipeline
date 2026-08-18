#!/usr/bin/env python
"""Build every service using its real context and Dockerfile.

Proves that ci/services.yaml matches the source tree and that all ten
services genuinely build, including the three different build shapes
(cartservice's split context, the Java/Gradle build, the Python and Node
multi-stage builds).

    python ci/build_all.py               # all ten
    python ci/build_all.py frontend      # one

Nothing is pushed. Images are tagged boutique/<name>:localtest.

Arguments are passed as a list to subprocess rather than through a shell,
because Git Bash on Windows rewrites anything that looks like a POSIX path
into a Windows path, which buildx then misparses.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def load_services(only):
    cfg = yaml.safe_load((REPO / "ci" / "services.yaml").read_text(encoding="utf-8"))
    services = cfg["services"]
    if only:
        wanted = set(only)
        services = [s for s in services if s["name"] in wanted]
        missing = wanted - {s["name"] for s in services}
        if missing:
            raise SystemExit("unknown service(s): %s" % ", ".join(sorted(missing)))
    return services


def build(service, log_dir, quiet):
    name = service["name"]
    cmd = [
        "docker", "build",
        "--file", str(REPO / service["dockerfile"]),
        "--tag", "boutique/%s:localtest" % name,
        str(REPO / service["context"]),
    ]
    log_path = log_dir / ("%s.log" % name)
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                              cwd=str(REPO))
    elapsed = time.time() - t0

    size = 0
    if proc.returncode == 0:
        out = subprocess.run(
            ["docker", "image", "inspect", "boutique/%s:localtest" % name,
             "--format", "{{.Size}}"],
            capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            size = int(out.stdout.strip())
    return proc.returncode == 0, elapsed, size, log_path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("services", nargs="*", help="build only these")
    ap.add_argument("--log-dir", default=str(REPO / ".build-logs"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    services = load_services(args.services)

    results = []
    started = time.time()
    for s in services:
        if not args.json:
            print("%-24s " % s["name"], end="", flush=True)
        ok, elapsed, size, log_path = build(s, log_dir, args.json)
        results.append({
            "name": s["name"], "ok": ok, "seconds": round(elapsed, 1),
            "megabytes": size // 1024 // 1024,
            "context": s["context"], "dockerfile": s["dockerfile"],
            "log": str(log_path),
        })
        if not args.json:
            if ok:
                print("PASS  %5.0fs  %5d MB  context=%s"
                      % (elapsed, size // 1024 // 1024, s["context"]))
            else:
                print("FAIL  %5.0fs  see %s" % (elapsed, log_path))

    passed = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    if args.json:
        print(json.dumps({"results": results,
                          "passed": len(passed),
                          "failed": len(failed)}, indent=2))
    else:
        print()
        print("built %d/%d in %.0fs" % (len(passed), len(results),
                                        time.time() - started))
        for r in failed:
            print("\n--- %s: last 25 lines ---" % r["name"])
            tail = Path(r["log"]).read_text(encoding="utf-8", errors="replace")
            print("\n".join(tail.splitlines()[-25:]))

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
