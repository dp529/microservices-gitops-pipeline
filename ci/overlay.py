#!/usr/bin/env python
"""Read and write the image reference in an environment overlay.

The overlay's `images:` entry is the deployment source of truth. This module
keeps it digest-pinned and is the only place that edits it, so the rules
about what a valid reference looks like live in exactly one file.

A pinned entry looks like:

    images:
      - name: paymentservice
        newName: <account>.dkr.ecr.<region>.amazonaws.com/boutique/paymentservice
        digest: sha256:abc...
        # tag: sha-9e7aaac   (traceability only, never the deploy source)
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENVIRONMENTS = ["dev", "staging", "prod"]

# Promotion is a one-way street between adjacent environments.
PROMOTION_PATH = {"dev": "staging", "staging": "prod"}

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class OverlayError(Exception):
    pass


def overlay_path(env, service):
    if env not in ENVIRONMENTS:
        raise OverlayError("unknown environment %r" % env)
    p = REPO / "gitops" / "overlays" / env / service / "kustomization.yaml"
    if not p.exists():
        raise OverlayError("no overlay for %s/%s (%s)" % (env, service, p))
    return p


def validate_digest(digest):
    if not DIGEST_RE.match(digest or ""):
        raise OverlayError(
            "%r is not an immutable digest; expected sha256: followed by 64 "
            "hex characters" % digest)
    return digest


def read_image(env, service):
    """Return (newName, digest, tag) from the overlay, any of which may be None.

    Parsed line-wise rather than with a YAML round-trip so that comments and
    formatting in the overlay survive a write.
    """
    text = overlay_path(env, service).read_text(encoding="utf-8")
    block = _images_block(text)
    if block is None:
        return None, None, None
    return (_field(block, "newName"),
            _field(block, "digest"),
            _field(block, "newTag"))


def _images_block(text):
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.rstrip() == "images:")
    except StopIteration:
        return None
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.startswith((" ", "\t", "-")):
            break
        end += 1
    return "\n".join(lines[start:end])


def _field(block, key):
    m = re.search(r"^\s*%s:\s*(\S+)\s*$" % re.escape(key), block, re.M)
    return m.group(1) if m else None


def write_image(env, service, new_name, digest, tag=None):
    """Replace the overlay's images: entry with a digest-pinned reference."""
    validate_digest(digest)
    path = overlay_path(env, service)
    text = path.read_text(encoding="utf-8")
    block = _images_block(text)
    if block is None:
        raise OverlayError("%s has no images: block" % path)

    trace = ""
    if tag:
        trace = ("\n    # tag %s is for human traceability only; the digest\n"
                 "    # above is what is deployed." % tag)

    replacement = (
        "images:\n"
        "  # Digest-pinned by CI. The digest, not a tag, is the deployment\n"
        "  # source of truth: the same digest is promoted unchanged from dev\n"
        "  # to staging to prod, so no environment rebuilds the image.\n"
        "  - name: %s\n"
        "    newName: %s\n"
        "    digest: %s%s" % (service, new_name, digest, trace)
    )
    path.write_text(text.replace(block, replacement), encoding="utf-8")
    return path


def promote(source_env, target_env, service):
    """Copy the image reference from one environment to the next.

    The digest is copied verbatim. Nothing is rebuilt, and the target's
    replica count, resources and namespace are untouched.
    """
    if source_env == target_env:
        raise OverlayError(
            "cannot promote %s to itself" % source_env)
    if source_env not in PROMOTION_PATH:
        raise OverlayError(
            "%s is not a promotion source; promotion flows %s"
            % (source_env, " -> ".join(ENVIRONMENTS)))
    expected = PROMOTION_PATH[source_env]
    if target_env != expected:
        raise OverlayError(
            "invalid promotion %s -> %s; %s promotes only to %s. "
            "Promotion flows %s and skipping an environment would deploy "
            "something no earlier environment ran."
            % (source_env, target_env, source_env, expected,
               " -> ".join(ENVIRONMENTS)))

    name, digest, tag = read_image(source_env, service)
    if not digest:
        raise OverlayError(
            "%s/%s has no digest to promote; it has not been published yet"
            % (source_env, service))
    validate_digest(digest)
    write_image(target_env, service, name, digest, tag)
    return name, digest


def is_pinned(env, service):
    _, digest, _ = read_image(env, service)
    return bool(digest and DIGEST_RE.match(digest))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", help="print an overlay's image reference")
    g.add_argument("env"); g.add_argument("service")

    s = sub.add_parser("set", help="pin an overlay to a digest")
    s.add_argument("env"); s.add_argument("service")
    s.add_argument("--image", required=True, help="repository without a tag")
    s.add_argument("--digest", required=True)
    s.add_argument("--tag", help="traceability tag, not used for deployment")

    p = sub.add_parser("promote", help="copy a digest to the next environment")
    p.add_argument("source"); p.add_argument("target")
    p.add_argument("services", nargs="+")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "get":
            n, d, t = read_image(args.env, args.service)
            print("newName: %s\ndigest:  %s\ntag:     %s" % (n, d, t))
        elif args.cmd == "set":
            path = write_image(args.env, args.service, args.image,
                               args.digest, args.tag)
            print("pinned %s/%s -> %s@%s" % (args.env, args.service,
                                             args.image, args.digest))
            print("  %s" % path.relative_to(REPO))
        elif args.cmd == "promote":
            for svc in args.services:
                name, digest = promote(args.source, args.target, svc)
                print("promoted %-24s %s -> %s  %s"
                      % (svc, args.source, args.target, digest))
            print("\n%d service(s) promoted. Digests copied unchanged; "
                  "nothing was rebuilt." % len(args.services))
    except OverlayError as e:
        sys.stderr.write("ERROR: %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
