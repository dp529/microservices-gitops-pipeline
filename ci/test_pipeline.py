#!/usr/bin/env python
"""End-to-end simulation of the release pipeline, offline.

Runs the real detector, the real overlay writer and the real promotion code
against a scratch copy of the repository, with digests standing in for what
ECR would return. Proves the pieces connect: a change selects services, those
services get pinned in dev, and the same digest reaches prod unchanged.

Nothing is built, pushed or deployed here.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import affected  # noqa: E402
import overlay  # noqa: E402

REAL_REPO = overlay.REPO
REGISTRY = "example-registry.invalid/ecr"
SERVICES, FANOUT, IGNORE = affected.load_config()

results = []


def check(title, condition, detail=""):
    results.append(bool(condition))
    print("%-5s %s" % ("PASS" if condition else "FAIL", title))
    if not condition and detail:
        print("        %s" % detail)


def fake_digest(seed):
    """A deterministic stand-in for what the registry would return."""
    import hashlib
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


class Scratch:
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pipeline-test-"))
        shutil.copytree(REAL_REPO / "gitops", self.tmp / "gitops")
        self.saved = overlay.REPO
        overlay.REPO = self.tmp
        return self.tmp

    def __exit__(self, *exc):
        overlay.REPO = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)


def detect(files):
    a, _, unknown, _ = affected.classify(files, SERVICES, FANOUT, IGNORE)
    return [m["name"] for m in affected.build_matrix(a, SERVICES)], unknown


def release(changed_files, tag):
    """detect -> (pretend build/push) -> pin dev. Returns {service: digest}."""
    names, _ = detect(changed_files)
    digests = {}
    for svc in names:
        d = fake_digest("%s|%s" % (svc, tag))
        overlay.write_image("dev", svc, "%s/boutique/%s" % (REGISTRY, svc), d, tag)
        digests[svc] = d
    return digests


def rendered_images(env, service):
    d = overlay.REPO / "gitops" / "overlays" / env / service
    r = subprocess.run(["kubectl", "kustomize", str(d)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return re.findall(r"^\s*-?\s*image:\s*(\S+)", r.stdout, re.M)


def main():
    print("End-to-end pipeline simulation (offline)\n")

    # ---- single service release -------------------------------------
    print("--- a paymentservice change ---")
    with Scratch():
        digests = release(["src/paymentservice/charge.js"], "sha-aaa1111")
        check("only paymentservice is selected", list(digests) == ["paymentservice"],
              str(list(digests)))

        pinned = [s["name"] for s in SERVICES if overlay.is_pinned("dev", s["name"])]
        check("exactly one dev overlay is pinned", pinned == ["paymentservice"],
              str(pinned))

        others = [s["name"] for s in SERVICES
                  if s["name"] != "paymentservice"
                  and "placeholder" in (overlay.REPO /
                      "gitops/overlays/dev" / s["name"] / "kustomization.yaml"
                  ).read_text(encoding="utf-8")]
        check("the other nine dev overlays keep their placeholder", len(others) == 9,
              "%d of 9" % len(others))

        d = digests["paymentservice"]
        overlay.promote("dev", "staging", "paymentservice")
        overlay.promote("staging", "prod", "paymentservice")
        _, sd, _ = overlay.read_image("staging", "paymentservice")
        _, pd, _ = overlay.read_image("prod", "paymentservice")
        check("the digest reaches prod unchanged", d == sd == pd,
              "%s / %s / %s" % (d[:20], sd[:20], pd[:20]))

        imgs = rendered_images("prod", "paymentservice")
        check("prod renders the digest-pinned reference",
              any("@%s" % d in i for i in imgs), str(imgs))

    # ---- cartservice: watch path vs build context --------------------
    print("\n--- a cartservice test-file change ---")
    with Scratch():
        names, _ = detect(["src/cartservice/tests/CartServiceTests.cs"])
        check("a change under the watch path selects cartservice",
              names == ["cartservice"], str(names))
        entry = next(s for s in SERVICES if s["name"] == "cartservice")
        check("the build context is src/cartservice/src, not the watch path",
              entry["context"] == "src/cartservice/src", entry["context"])
        check("watch path and build context are genuinely different",
              entry["watch"] != entry["context"])

        digests = release(["src/cartservice/tests/CartServiceTests.cs"], "sha-bbb2222")
        overlay.promote("dev", "staging", "cartservice")
        overlay.promote("staging", "prod", "cartservice")
        imgs = rendered_images("prod", "cartservice")
        check("prod cartservice renders a digest-pinned application image",
              any("boutique/cartservice@sha256:" in i for i in imgs), str(imgs))
        check("redis is pinned by digest and is not a CI artifact",
              any(i.startswith("redis@sha256:") for i in imgs), str(imgs))

    # ---- proto fan-out ----------------------------------------------
    print("\n--- a protos/demo.proto change ---")
    with Scratch():
        digests = release(["protos/demo.proto"], "sha-ccc3333")
        check("all ten services are selected", len(digests) == 10, str(len(digests)))
        check("every service gets its own distinct digest",
              len(set(digests.values())) == 10)

        for svc in digests:
            overlay.promote("dev", "staging", svc)
        promoted = {s: overlay.read_image("staging", s)[1] for s in digests}
        check("all ten promote to staging with matching digests",
              promoted == digests, "mismatches: %s"
              % [s for s in digests if promoted[s] != digests[s]])

        for svc in digests:
            overlay.promote("staging", "prod", svc)
        in_prod = {s: overlay.read_image("prod", s)[1] for s in digests}
        check("all ten reach prod with the same digests, none dropped",
              in_prod == digests,
              "mismatches: %s" % [s for s in digests if in_prod[s] != digests[s]])

    # ---- no rebuild between environments ----------------------------
    print("\n--- promotion does not rebuild ---")
    with Scratch():
        d1 = release(["src/frontend/main.go"], "sha-ddd4444")["frontend"]
        overlay.promote("dev", "staging", "frontend")
        # A second CI run publishes a NEW digest to dev only.
        d2 = release(["src/frontend/main.go"], "sha-eee5555")["frontend"]
        _, staging_digest, _ = overlay.read_image("staging", "frontend")
        check("a new dev build does not disturb staging", staging_digest == d1,
              "staging=%s expected=%s" % (staging_digest[:20], d1[:20]))
        check("dev moved on independently", d2 != d1)

        overlay.promote("dev", "staging", "frontend")
        _, staging_now, _ = overlay.read_image("staging", "frontend")
        check("staging advances only when promoted", staging_now == d2)

    # ---- non-application changes ------------------------------------
    print("\n--- changes that must not release ---")
    for label, files in [
        ("docs only", ["docs/deployable-units.md"]),
        ("gitops only", ["gitops/overlays/prod/frontend/kustomization.yaml"]),
        ("README only", ["README.md"]),
        ("CI tooling only", ["ci/overlay.py"]),
    ]:
        names, _ = detect(files)
        check("%s releases nothing" % label, names == [], str(names))

    names, unknown = detect(["src/mystery-service/main.go"])
    check("an unknown service directory is reported, not guessed",
          names == [] and bool(unknown), str(unknown))

    # ---- prior validation still holds -------------------------------
    print("\n--- existing validation ---")
    for label, script in [
        ("semantic validation of the 30 overlays", "gitops/validate/semantic_check.py"),
        ("ApplicationSet generates 30 Applications", "gitops/validate/appset_check.py"),
        ("affected-service detector", "ci/test_affected.py"),
    ]:
        r = subprocess.run([sys.executable, str(REAL_REPO / script)],
                           capture_output=True, text=True, cwd=str(REAL_REPO))
        check("%s still passes" % label, r.returncode == 0,
              r.stdout.strip().splitlines()[-1] if r.stdout else r.stderr[:200])

    print()
    passed = sum(1 for r in results if r)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
