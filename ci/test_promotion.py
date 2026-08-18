#!/usr/bin/env python
"""Tests for digest pinning and promotion.

Every test runs against a scratch copy of gitops/overlays, so the real
overlays are never modified.

    python ci/test_promotion.py
    python ci/test_promotion.py --compare dev staging frontend
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import overlay  # noqa: E402

REAL_REPO = overlay.REPO
# Deliberately not a real-looking account id; the secret scan below
# rejects any 12-digit ECR host anywhere in the repository.
REGISTRY = "example-registry.invalid/ecr"
D1 = "sha256:" + "a1" * 32
D2 = "sha256:" + "b2" * 32

results = []


def check(title, condition, detail=""):
    results.append(bool(condition))
    print("%-5s %s" % ("PASS" if condition else "FAIL", title))
    if not condition and detail:
        print("        %s" % detail)
    return condition


def expect_error(title, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except overlay.OverlayError as e:
        return check(title, True) or print("        rejected: %s" % e) or True
    return check(title, False, "expected an OverlayError but the call succeeded")


class Scratch:
    """A throwaway copy of the repository's gitops tree."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="promotion-test-"))
        shutil.copytree(REAL_REPO / "gitops", self.tmp / "gitops")
        self.saved = overlay.REPO
        overlay.REPO = self.tmp
        return self.tmp

    def __exit__(self, *exc):
        overlay.REPO = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)


def services_in(env, root):
    return sorted(p.name for p in (root / "gitops" / "overlays" / env).iterdir()
                  if p.is_dir())


def run_tests():
    print("Digest pinning and promotion tests\n")

    # ---- pinning ----------------------------------------------------
    with Scratch() as root:
        overlay.write_image("dev", "paymentservice",
                            "%s/boutique/paymentservice" % REGISTRY, D1,
                            "sha-abc1234")
        name, digest, _ = overlay.read_image("dev", "paymentservice")
        check("digest is written to the dev overlay", digest == D1, digest)
        check("newName carries the ECR repository",
              name == "%s/boutique/paymentservice" % REGISTRY, name)

        text = (root / "gitops/overlays/dev/paymentservice/kustomization.yaml"
                ).read_text(encoding="utf-8")
        check("mutable placeholder tag is gone",
              "dev-placeholder" not in text)
        check("traceability tag is recorded as a comment, not as newTag",
              "sha-abc1234" in text and "newTag:" not in text)

        # untouched services must be byte-identical
        untouched = 0
        for svc in services_in("dev", root):
            if svc == "paymentservice":
                continue
            a = (root / "gitops/overlays/dev" / svc / "kustomization.yaml").read_bytes()
            b = (REAL_REPO / "gitops/overlays/dev" / svc / "kustomization.yaml").read_bytes()
            if a == b:
                untouched += 1
        check("the other nine dev overlays are untouched", untouched == 9,
              "%d/9 identical" % untouched)

    # ---- invalid digests --------------------------------------------
    with Scratch():
        expect_error("a mutable tag is rejected as a digest",
                     overlay.write_image, "dev", "frontend",
                     "repo/frontend", "latest")
        expect_error("a truncated digest is rejected",
                     overlay.write_image, "dev", "frontend",
                     "repo/frontend", "sha256:abc")
        expect_error("an empty digest is rejected",
                     overlay.write_image, "dev", "frontend",
                     "repo/frontend", "")

    # ---- promotion direction ----------------------------------------
    with Scratch():
        expect_error("dev -> prod is rejected (skips staging)",
                     overlay.promote, "dev", "prod", "frontend")
        expect_error("staging -> dev is rejected (backwards)",
                     overlay.promote, "staging", "dev", "frontend")
        expect_error("dev -> dev is rejected (same environment)",
                     overlay.promote, "dev", "dev", "frontend")
        expect_error("prod -> anywhere is rejected (end of the line)",
                     overlay.promote, "prod", "staging", "frontend")

    # ---- promotion preserves the digest exactly ---------------------
    with Scratch():
        overlay.write_image("dev", "frontend",
                            "%s/boutique/frontend" % REGISTRY, D1, "sha-abc1234")
        overlay.promote("dev", "staging", "frontend")
        _, dev_d, _ = overlay.read_image("dev", "frontend")
        _, stg_d, _ = overlay.read_image("staging", "frontend")
        check("dev -> staging copies the digest byte for byte", dev_d == stg_d,
              "%s vs %s" % (dev_d, stg_d))

        overlay.promote("staging", "prod", "frontend")
        _, prd_d, _ = overlay.read_image("prod", "frontend")
        check("staging -> prod copies the digest byte for byte", stg_d == prd_d,
              "%s vs %s" % (stg_d, prd_d))
        check("the same digest reaches prod that was built for dev",
              dev_d == prd_d == D1)

    # ---- promotion cannot invent a digest ---------------------------
    with Scratch():
        expect_error("promoting a service dev never published is rejected",
                     overlay.promote, "dev", "staging", "adservice")

    # ---- promotion does not disturb environment settings ------------
    with Scratch() as root:
        before = (root / "gitops/overlays/prod/frontend/kustomization.yaml"
                  ).read_text(encoding="utf-8")
        replicas_before = re.search(r"count: (\d+)", before).group(1)
        overlay.write_image("dev", "frontend",
                            "%s/boutique/frontend" % REGISTRY, D1)
        overlay.promote("dev", "staging", "frontend")
        overlay.promote("staging", "prod", "frontend")
        after = (root / "gitops/overlays/prod/frontend/kustomization.yaml"
                 ).read_text(encoding="utf-8")
        replicas_after = re.search(r"count: (\d+)", after).group(1)
        check("prod keeps its replica count through promotion",
              replicas_before == replicas_after == "3",
              "%s -> %s" % (replicas_before, replicas_after))
        check("prod keeps its namespace through promotion",
              "namespace: boutique-prod" in after)

    # ---- multi-service release --------------------------------------
    with Scratch() as root:
        every = services_in("dev", root)
        check("ten services are present", len(every) == 10, str(len(every)))
        for svc in every:
            overlay.write_image("dev", svc,
                                "%s/boutique/%s" % (REGISTRY, svc), D1,
                                "sha-proto01")
        promoted = 0
        for svc in every:
            overlay.promote("dev", "staging", svc)
            promoted += 1
        check("a ten-service release promotes all ten", promoted == 10)
        digests = set()
        for svc in every:
            _, d, _ = overlay.read_image("staging", svc)
            digests.add(d)
        check("no service was silently dropped from the release",
              digests == {D1}, str(digests))

    # ---- a second release replaces, not appends ---------------------
    with Scratch():
        overlay.write_image("dev", "frontend", "%s/boutique/frontend" % REGISTRY, D1)
        overlay.write_image("dev", "frontend", "%s/boutique/frontend" % REGISTRY, D2)
        _, d, _ = overlay.read_image("dev", "frontend")
        check("a newer digest replaces the previous one", d == D2, d)
        text = (overlay.REPO / "gitops/overlays/dev/frontend/kustomization.yaml"
                ).read_text(encoding="utf-8")
        check("only one digest line remains",
              text.count("digest:") == 1, str(text.count("digest:")))

    # ---- rendering still works with a digest ------------------------
    with Scratch() as root:
        overlay.write_image("dev", "frontend",
                            "%s/boutique/frontend" % REGISTRY, D1)
        r = subprocess.run(
            ["kubectl", "kustomize", str(root / "gitops/overlays/dev/frontend")],
            capture_output=True, text=True)
        ok = r.returncode == 0
        check("a digest-pinned overlay still renders", ok,
              r.stderr.strip()[:200])
        if ok:
            check("the rendered image is digest-pinned",
                  "@%s" % D1 in r.stdout,
                  [l for l in r.stdout.splitlines() if "image:" in l])
            check("no mutable tag survives into the rendered output",
                  "dev-placeholder" not in r.stdout)

    # ---- redis is not treated as an application artifact ------------
    cfg = (REAL_REPO / "ci" / "services.yaml").read_text(encoding="utf-8")
    check("redis is not a build target in services.yaml",
          "redis" not in cfg.split("ignore:")[0])
    base = (REAL_REPO / "gitops/base/cartservice/redis-deployment.yaml"
            ).read_text(encoding="utf-8")
    check("redis stays bundled with cartservice", "redis-cart" in base)

    # ---- no credentials committed -----------------------------------
    leaked = []
    secret_patterns = [
        (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
        (re.compile(r"aws_secret_access_key\s*=", re.I), "AWS secret key"),
        (re.compile(r"\b\d{12}\.dkr\.ecr\."), "hardcoded AWS account id"),
    ]
    for path in REAL_REPO.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REAL_REPO).as_posix()
        if rel.startswith((".git/", ".build-logs/", "src/")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern, label in secret_patterns:
            for m in pattern.finditer(text):
                leaked.append("%s: %s (%s)" % (rel, m.group(0), label))
    check("no AWS credentials or account ids are committed", not leaked,
          "; ".join(leaked[:4]))

    print()
    passed = sum(1 for r in results if r)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


def compare(source, target, services):
    """Assert the two environments carry identical digests (used by CI)."""
    bad = 0
    for svc in services:
        _, sd, _ = overlay.read_image(source, svc)
        _, td, _ = overlay.read_image(target, svc)
        if sd != td or not sd:
            print("MISMATCH %-24s %s=%s %s=%s" % (svc, source, sd, target, td))
            bad += 1
        else:
            print("identical %-24s %s" % (svc, sd))
    if bad:
        print("\n%d service(s) differ; promotion must copy digests, not "
              "regenerate them." % bad)
        return 1
    print("\n%d service(s) carry the same digest in %s and %s. Nothing was "
          "rebuilt." % (len(services), source, target))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", nargs="+", metavar="ARG",
                    help="SOURCE TARGET SERVICE...")
    a = ap.parse_args()
    if a.compare:
        sys.exit(compare(a.compare[0], a.compare[1], a.compare[2:]))
    sys.exit(run_tests())
