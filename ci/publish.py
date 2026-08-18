#!/usr/bin/env python
"""Record published image digests into the dev overlays.

CI builds and pushes an image, reads back the digest the registry assigned,
and calls this with the result. Only the services named are touched; the
other overlays are not rewritten.

    python ci/publish.py \
        --registry <account>.dkr.ecr.<region>.amazonaws.com \
        --tag sha-9e7aaac \
        paymentservice=sha256:abc... cartservice=sha256:def...

The registry never appears in the repository as a literal: it is supplied by
the workflow from repository variables, so no account id is committed.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import overlay  # noqa: E402

# One ECR repository per deployable service, under a shared prefix. Separate
# repositories mean per-service lifecycle policies, per-service scan findings
# and per-service IAM scoping; a single repository with the service in the tag
# would give up all three.
REPO_PREFIX = "boutique"


def image_name(registry, service):
    return "%s/%s/%s" % (registry.rstrip("/"), REPO_PREFIX, service)


def parse_pair(pair):
    if "=" not in pair:
        raise SystemExit("expected service=sha256:... but got %r" % pair)
    service, digest = pair.split("=", 1)
    return service.strip(), digest.strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--registry", required=True,
                    help="registry host, e.g. <account>.dkr.ecr.<region>.amazonaws.com")
    ap.add_argument("--tag", help="traceability tag, not the deploy source")
    ap.add_argument("--env", default="dev",
                    help="environment to update (default: dev; promotion "
                         "moves digests onward from there)")
    ap.add_argument("pairs", nargs="+", metavar="SERVICE=DIGEST")
    args = ap.parse_args(argv)

    if args.env != "dev":
        # Publication targets dev by design. Staging and prod receive digests
        # only by promotion, so that a digest cannot enter a later
        # environment without having existed in an earlier one.
        sys.stderr.write(
            "ERROR: publish writes to dev only. %s receives digests through "
            "promotion (ci/overlay.py promote).\n" % args.env)
        return 1

    updated = []
    try:
        for pair in args.pairs:
            service, digest = parse_pair(pair)
            overlay.validate_digest(digest)
            name = image_name(args.registry, service)
            path = overlay.write_image(args.env, service, name, digest, args.tag)
            updated.append((service, name, digest, path))
    except overlay.OverlayError as e:
        sys.stderr.write("ERROR: %s\n" % e)
        return 1

    for service, name, digest, path in updated:
        print("%-24s %s@%s" % (service, name, digest))
        print("    %s" % path.relative_to(overlay.REPO).as_posix())
    print("\n%d dev overlay(s) updated. No other environment was touched."
          % len(updated))
    return 0


if __name__ == "__main__":
    sys.exit(main())
