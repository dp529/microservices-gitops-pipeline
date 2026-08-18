#!/usr/bin/env python
"""Semantic validation of the rendered Kustomize overlays.

`kustomize build` proves the YAML parses. This proves it means something:
that selectors match the pods they claim, that ports line up end to end,
that services address each other correctly, and that the environments
differ only where they are supposed to.

Local only. Renders with `kubectl kustomize` and contacts no cluster.
"""
import copy
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAYS = ROOT / "overlays"
ENVS = ["dev", "staging", "prod"]
SERVICES = [
    "frontend", "cartservice", "productcatalogservice", "currencyservice",
    "paymentservice", "shippingservice", "emailservice", "checkoutservice",
    "recommendationservice", "adservice",
]

# Ground truth from Phase 1 discovery of the real upstream source.
EXPECTED_PORTS = {
    "frontend":              {"container": 8080,  "service": 80},
    "cartservice":           {"container": 7070,  "service": 7070},
    "productcatalogservice": {"container": 3550,  "service": 3550},
    "currencyservice":       {"container": 7000,  "service": 7000},
    "paymentservice":        {"container": 50051, "service": 50051},
    "shippingservice":       {"container": 50051, "service": 50051},
    "emailservice":          {"container": 8080,  "service": 5000},
    "checkoutservice":       {"container": 5050,  "service": 5050},
    "recommendationservice": {"container": 8080,  "service": 8080},
    "adservice":             {"container": 9555,  "service": 9555},
    "redis-cart":            {"container": 6379,  "service": 6379},
}

EXPECTED_ENV = {
    "dev":     {"namespace": "boutique-dev",     "replicas": 1},
    "staging": {"namespace": "boutique-staging", "replicas": 2},
    "prod":    {"namespace": "boutique-prod",    "replicas": 3},
}

ADDR_VAR = re.compile(r"_SERVICE_ADDR$|^REDIS_ADDR$")
ADDR_VALUE = re.compile(r"^([a-z][a-z0-9-]*):(\d+)$")

# Containers CI never rewrites, so they may legitimately carry a plain tag.
NOT_CI_MANAGED = {"redis"}

defects = []
checks_run = 0


def defect(env, svc, check, msg):
    defects.append((env, svc, check, msg))


def ok(n=1):
    global checks_run
    checks_run += n


def q(v):
    """Parse a Kubernetes quantity into a comparable number."""
    v = str(v)
    units = {"m": 1e-3, "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30,
             "K": 1e3, "M": 1e6, "G": 1e9}
    for u, mult in sorted(units.items(), key=lambda x: -len(x[0])):
        if v.endswith(u):
            return float(v[:-len(u)]) * mult
    return float(v)


def render(env, svc):
    out = subprocess.run(
        ["kubectl", "kustomize", str(OVERLAYS / env / svc)],
        capture_output=True, text=True, check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def pod_labels(dep):
    return dep["spec"]["template"]["metadata"].get("labels", {})


def containers(dep):
    return dep["spec"]["template"]["spec"]["containers"]


def check_overlay(env, svc, registry):
    docs = render(env, svc)
    by_kind = {}
    for d in docs:
        by_kind.setdefault(d["kind"], []).append(d)
    deps = by_kind.get("Deployment", [])
    svcs = by_kind.get("Service", [])
    sas = by_kind.get("ServiceAccount", [])
    ns_expected = EXPECTED_ENV[env]["namespace"]

    # --- namespace present and correct on every resource -------------
    for d in docs:
        got = d["metadata"].get("namespace")
        if got != ns_expected:
            defect(env, svc, "namespace",
                   "%s/%s namespace=%r expected %r"
                   % (d["kind"], d["metadata"]["name"], got, ns_expected))
        ok()

    # --- Deployment selector must match its own pod template ---------
    for dep in deps:
        name = dep["metadata"]["name"]
        sel = dep["spec"]["selector"].get("matchLabels", {})
        labels = pod_labels(dep)
        if not sel:
            defect(env, svc, "selector", "Deployment/%s has empty selector" % name)
        for k, v in sel.items():
            if labels.get(k) != v:
                defect(env, svc, "selector",
                       "Deployment/%s selects %s=%s but its pod template has "
                       "%s=%r - owns no pods" % (name, k, v, k, labels.get(k)))
        ok()

    # --- Service selector must resolve to exactly one workload -------
    for s in svcs:
        sname = s["metadata"]["name"]
        sel = s["spec"].get("selector", {})
        if not sel:
            defect(env, svc, "svc-selector", "Service/%s has no selector" % sname)
            continue
        matched = [d for d in deps
                   if all(pod_labels(d).get(k) == v for k, v in sel.items())]
        if not matched:
            defect(env, svc, "svc-selector",
                   "Service/%s selector %s matches no pod template - "
                   "endpoints would be empty" % (sname, sel))
        elif len(matched) > 1:
            defect(env, svc, "svc-selector",
                   "Service/%s selector %s matches multiple workloads %s - "
                   "cross-wired" % (sname, sel,
                                    [m["metadata"]["name"] for m in matched]))
        ok()

        # --- targetPort must be a real containerPort of that workload
        if matched:
            target = matched[0]
            cports = {p["containerPort"] for c in containers(target)
                      for p in c.get("ports", [])}
            for port in s["spec"]["ports"]:
                tp = port.get("targetPort", port["port"])
                if isinstance(tp, int) and tp not in cports:
                    defect(env, svc, "targetPort",
                           "Service/%s targetPort=%s is not a containerPort of "
                           "%s %s - traffic blackholes"
                           % (sname, tp, target["metadata"]["name"], sorted(cports)))
                ok()

        # --- declared Service port matches Phase 1 -------------------
        if sname in EXPECTED_PORTS:
            want = EXPECTED_PORTS[sname]["service"]
            got = s["spec"]["ports"][0]["port"]
            if got != want:
                defect(env, svc, "port-drift",
                       "Service/%s port=%s but Phase 1 discovered %s"
                       % (sname, got, want))
            ok()
            registry.setdefault(env, {})[sname] = got

    # --- per-workload checks -----------------------------------------
    for dep in deps:
        dname = dep["metadata"]["name"]
        pod_sc = dep["spec"]["template"]["spec"].get("securityContext", {})

        for c in containers(dep):
            cports = {p["containerPort"] for p in c.get("ports", [])}

            # probes must target a declared containerPort
            for probe_kind in ("readinessProbe", "livenessProbe"):
                probe = c.get(probe_kind)
                if not probe:
                    defect(env, svc, "probe-missing",
                           "%s/%s has no %s" % (dname, c["name"], probe_kind))
                    continue
                handler = next((k for k in ("grpc", "httpGet", "tcpSocket", "exec")
                                if k in probe), None)
                if handler is None:
                    defect(env, svc, "probe",
                           "%s %s has no handler" % (dname, probe_kind))
                    continue
                if handler != "exec":
                    pport = probe[handler].get("port")
                    if isinstance(pport, int) and pport not in cports:
                        defect(env, svc, "probe-port",
                               "%s/%s %s targets port %s, not in containerPorts "
                               "%s - pod never becomes Ready"
                               % (dname, c["name"], probe_kind, pport, sorted(cports)))
                ok()

            # containerPort matches Phase 1
            if dname in EXPECTED_PORTS and c.get("ports"):
                want = EXPECTED_PORTS[dname]["container"]
                got = c["ports"][0]["containerPort"]
                if got != want:
                    defect(env, svc, "port-drift",
                           "%s containerPort=%s but Phase 1 discovered %s"
                           % (dname, got, want))
                ok()

            # securityContext hardening
            csc = c.get("securityContext", {})
            if pod_sc.get("runAsNonRoot") is not True:
                defect(env, svc, "securityContext",
                       "%s pod runAsNonRoot is not true" % dname)
            if not pod_sc.get("runAsUser"):
                defect(env, svc, "securityContext",
                       "%s pod runAsUser=%r must be non-zero"
                       % (dname, pod_sc.get("runAsUser")))
            if csc.get("privileged") is not False:
                defect(env, svc, "securityContext",
                       "%s/%s privileged is not explicitly false" % (dname, c["name"]))
            if csc.get("allowPrivilegeEscalation") is not False:
                defect(env, svc, "securityContext",
                       "%s/%s allowPrivilegeEscalation is not false"
                       % (dname, c["name"]))
            if csc.get("readOnlyRootFilesystem") is not True:
                defect(env, svc, "securityContext",
                       "%s/%s readOnlyRootFilesystem is not true" % (dname, c["name"]))
            if "ALL" not in (csc.get("capabilities") or {}).get("drop", []):
                defect(env, svc, "securityContext",
                       "%s/%s does not drop ALL capabilities" % (dname, c["name"]))
            ok(6)

            # resource governance
            res = c.get("resources", {})
            for section in ("requests", "limits"):
                for dim in ("cpu", "memory"):
                    if not res.get(section, {}).get(dim):
                        defect(env, svc, "resources",
                               "%s/%s missing %s.%s" % (dname, c["name"], section, dim))
                    ok()
            try:
                for dim in ("cpu", "memory"):
                    if q(res["limits"][dim]) < q(res["requests"][dim]):
                        defect(env, svc, "resources",
                               "%s/%s %s limit is below its request"
                               % (dname, c["name"], dim))
                    ok()
            except (KeyError, ValueError):
                pass

            # image policy
            img = c["image"]
            tail = img.split("/")[-1]
            if img.endswith(":latest") or (":" not in tail and "@" not in tail):
                defect(env, svc, "image",
                       "%s/%s image %r is :latest or untagged"
                       % (dname, c["name"], img))
            if "@sha256:" not in img and c["name"] not in NOT_CI_MANAGED:
                if not img.endswith(":%s-placeholder" % env):
                    defect(env, svc, "image",
                           "%s/%s image %r is neither a digest nor the "
                           "expected %s-placeholder" % (dname, c["name"], img, env))
            ok(2)

        # replicas
        want_replicas = EXPECTED_ENV[env]["replicas"]
        got_replicas = dep["spec"].get("replicas")
        if dname == "redis-cart":
            if got_replicas != 1:
                defect(env, svc, "replicas",
                       "redis-cart replicas=%s; a single-instance emptyDir "
                       "Redis must stay at 1" % got_replicas)
        elif got_replicas != want_replicas:
            defect(env, svc, "replicas",
                   "%s replicas=%s expected %s for %s"
                   % (dname, got_replicas, want_replicas, env))
        ok()

        # ServiceAccount referenced must be created by this same overlay
        sa_ref = dep["spec"]["template"]["spec"].get("serviceAccountName")
        if sa_ref and sa_ref not in {a["metadata"]["name"] for a in sas}:
            defect(env, svc, "serviceaccount",
                   "%s references ServiceAccount/%s which this overlay does "
                   "not create" % (dname, sa_ref))
        ok()

    return docs


def check_addresses(env, docs_by_svc, registry):
    """Every service-address env var must name a Service that exists in
    this environment, on the port it actually exposes."""
    known = registry.get(env, {})
    for svc, docs in docs_by_svc.items():
        for d in docs:
            if d["kind"] != "Deployment":
                continue
            for c in d["spec"]["template"]["spec"]["containers"]:
                for e in c.get("env", []):
                    if not ADDR_VAR.search(e.get("name", "")):
                        continue
                    m = ADDR_VALUE.match(str(e.get("value", "")))
                    if not m:
                        defect(env, svc, "address",
                               "%s=%r is not host:port" % (e["name"], e.get("value")))
                        continue
                    host, port = m.group(1), int(m.group(2))
                    if host not in known:
                        defect(env, svc, "address",
                               "%s points at %s:%s but no Service named %s "
                               "exists in %s" % (e["name"], host, port, host, env))
                    elif known[host] != port:
                        defect(env, svc, "address",
                               "%s=%s:%s but Service/%s exposes %s - "
                               "connection refused"
                               % (e["name"], host, port, host, known[host]))
                    ok()


def check_drift(rendered):
    """dev/staging/prod must differ ONLY in namespace, replica count,
    image tag, and redis resource sizing. Anything else is drift."""

    def normalize(docs):
        out = []
        ordered = sorted(copy.deepcopy(docs),
                         key=lambda x: (x["kind"], x["metadata"]["name"]))
        for d in ordered:
            d["metadata"].pop("namespace", None)
            if d["kind"] == "Deployment":
                d["spec"].pop("replicas", None)
                for c in d["spec"]["template"]["spec"]["containers"]:
                    c["image"] = re.sub(r":(dev|staging|prod)-placeholder$",
                                        ":TAG", c["image"])
                    if c["name"] == "redis":
                        c.pop("resources", None)
            out.append(d)
        return out

    for svc in SERVICES:
        base = normalize(rendered["dev"][svc])
        for env in ("staging", "prod"):
            other = normalize(rendered[env][svc])
            if base != other:
                for a, b in zip(base, other):
                    if a != b:
                        keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
                        defect(env, svc, "drift",
                               "%s/%s differs from dev beyond namespace/"
                               "replicas/tag: %s"
                               % (a["kind"], a["metadata"]["name"], keys))
            ok()


def main():
    registry = {}
    rendered = {}
    print("Semantic validation of 30 rendered overlays (local only)\n")
    for env in ENVS:
        rendered[env] = {}
        for svc in SERVICES:
            rendered[env][svc] = check_overlay(env, svc, registry)

    for env in ENVS:
        check_addresses(env, rendered[env], registry)
    check_drift(rendered)

    print("Semantic assertions run: %d" % checks_run)
    print("Overlays checked:        %d" % (len(ENVS) * len(SERVICES)))
    print("Defects found:           %d\n" % len(defects))

    if defects:
        print("DEFECTS")
        print("-" * 72)
        for env, svc, check, msg in defects:
            print("  [%s] %s/%s\n      %s" % (check, env, svc, msg))
        return 1
    print("PASS - all semantic checks clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
