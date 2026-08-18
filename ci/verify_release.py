#!/usr/bin/env python
"""Verify a release change before it is committed.

Answers the questions that matter about a working tree that CI has just
modified:

  - are the changed overlays all in the environment we expected?
  - is every changed image reference an immutable digest?
  - did anything outside the overlays change?
  - do the overlays still render?

    python ci/verify_release.py --expect-env dev
    python ci/verify_release.py --expect-env staging --expect-services frontend
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import overlay  # noqa: E402

REPO = overlay.REPO
OVERLAY_RE = re.compile(
    r"^gitops/overlays/(dev|staging|prod)/([a-z0-9-]+)/kustomization\.yaml$")

problems = []


def fail(msg):
    problems.append(msg)


def changed_paths():
    out = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only"],
                         capture_output=True, text=True, check=True).stdout
    staged = subprocess.run(["git", "-C", str(REPO), "diff", "--cached",
                             "--name-only"],
                            capture_output=True, text=True, check=True).stdout
    paths = {l.strip().replace("\\", "/")
             for l in (out + staged).splitlines() if l.strip()}
    return sorted(paths)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--expect-env", required=True,
                    choices=overlay.ENVIRONMENTS)
    ap.add_argument("--expect-services", nargs="*",
                    help="if given, exactly these services must have changed")
    args = ap.parse_args(argv)

    paths = changed_paths()
    if not paths:
        print("No changes in the working tree.")
        return 0

    print("Changed files: %d" % len(paths))
    for p in paths:
        print("    %s" % p)
    print()

    touched = {}
    for p in paths:
        m = OVERLAY_RE.match(p)
        if not m:
            fail("%s is not an environment overlay; a release commit should "
                 "touch only gitops/overlays/<env>/<service>/kustomization.yaml"
                 % p)
            continue
        env, service = m.group(1), m.group(2)
        if env != args.expect_env:
            fail("%s changes the %s environment, but this release targets %s"
                 % (p, env, args.expect_env))
        touched.setdefault(env, set()).add(service)

    services = sorted(touched.get(args.expect_env, set()))

    if args.expect_services is not None:
        expected = sorted(args.expect_services)
        if services != expected:
            fail("expected exactly %s to change but %s did"
                 % (expected, services))

    # every touched overlay must now be digest-pinned
    for service in services:
        name, digest, tag = overlay.read_image(args.expect_env, service)
        if not digest:
            fail("%s/%s has no digest; deployment must not be driven by a "
                 "mutable tag" % (args.expect_env, service))
            continue
        try:
            overlay.validate_digest(digest)
        except overlay.OverlayError as e:
            fail("%s/%s: %s" % (args.expect_env, service, e))
        if not name:
            fail("%s/%s has a digest but no newName" % (args.expect_env, service))

    # the overlays must still render
    for service in services:
        d = REPO / "gitops" / "overlays" / args.expect_env / service
        r = subprocess.run(["kubectl", "kustomize", str(d)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            fail("%s/%s no longer renders: %s"
                 % (args.expect_env, service, r.stderr.strip().splitlines()[:1]))
        elif "@sha256:" not in r.stdout:
            fail("%s/%s renders without a digest in its image reference"
                 % (args.expect_env, service))

    print("Environment:        %s" % args.expect_env)
    print("Services changed:   %d (%s)" % (len(services), ", ".join(services) or "-"))
    print("Untouched services: %d"
          % (len([d for d in (REPO / "gitops" / "overlays" / args.expect_env).iterdir()
                  if d.is_dir()]) - len(services)))
    print()

    if problems:
        print("PROBLEMS")
        print("-" * 68)
        for p in problems:
            print("  %s" % p)
        return 1
    print("PASS - release change is confined to %s and fully digest-pinned"
          % args.expect_env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
