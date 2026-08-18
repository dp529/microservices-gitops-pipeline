#!/usr/bin/env python
"""Offline validation of the Argo CD ApplicationSet and AppProject.

Argo CD is not installed and no cluster is reachable, so this does two
things a schema check cannot:

  1. Validates the manifests structurally against what Argo CD requires.
  2. Simulates the matrix generator against the real repository layout,
     expanding it into the Applications it would actually produce, and
     checks each one against the AppProject and against the overlays that
     already exist on disk.

Contacts nothing.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
ARGOCD = ROOT / "argocd"
OVERLAYS = ROOT / "overlays"

problems = []
checks = 0


def fail(category, msg):
    problems.append((category, msg))


def ok(n=1):
    global checks
    checks += n


def load(name):
    return yaml.safe_load((ARGOCD / name).read_text(encoding="utf-8"))


def discover(environment):
    """Reproduce the git directory generator:
        directories: [{path: gitops/overlays/{{environment}}/*}]
    Argo CD matches directories containing manifests; every overlay here
    holds a kustomization.yaml."""
    env_dir = OVERLAYS / environment
    if not env_dir.is_dir():
        return []
    return sorted(
        d.name for d in env_dir.iterdir()
        if d.is_dir() and (d / "kustomization.yaml").exists()
    )


def go_template(text, params):
    """Render a Go template the way the ApplicationSet controller does.

    Only the subset used here is supported: dotted field access, `if/else`,
    `eq`, and whitespace trimming with {{- -}}. A missing key raises, which
    mirrors goTemplateOptions: [missingkey=error].
    """
    def lookup(expr):
        cur = params
        for part in expr.strip().lstrip(".").split("."):
            if not isinstance(cur, dict) or part not in cur:
                raise KeyError(expr)
            cur = cur[part]
        return cur

    # {{- if eq .x "y" }} A {{- else }} B {{- end }}
    cond = re.compile(
        r"\{\{-?\s*if\s+eq\s+(\.[\w.]+)\s+\"([^\"]*)\"\s*-?\}\}"
        r"(.*?)"
        r"(?:\{\{-?\s*else\s*-?\}\}(.*?))?"
        r"\{\{-?\s*end\s*-?\}\}",
        re.S,
    )

    def resolve_cond(m):
        var, want, then, other = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        return then if str(lookup(var)) == want else other

    prev = None
    out = text
    while prev != out:
        prev = out
        out = cond.sub(resolve_cond, out)

    # {{ .a.b }}
    out = re.sub(r"\{\{-?\s*(\.[\w.]+)\s*-?\}\}",
                 lambda m: str(lookup(m.group(1))), out)
    return out


def fasttemplate(text, params):
    """Legacy non-Go substitution, for comparison only."""
    out = text
    for k, v in params.items():
        out = out.replace("{{%s}}" % k, str(v))
    return out


def merge(base, patch):
    """Strategic-ish merge of the templatePatch result over the Application."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


def expand(appset):
    """Run the matrix generator and return the generated Applications."""
    spec = appset["spec"]
    go = spec.get("goTemplate", False)
    gens = spec["generators"]
    if len(gens) != 1 or "matrix" not in gens[0]:
        fail("generator", "expected exactly one matrix generator")
        return []
    inner = gens[0]["matrix"]["generators"]
    if len(inner) != 2:
        fail("generator", "matrix should combine exactly two generators")
        return []

    lists = [g for g in inner if "list" in g]
    gits = [g for g in inner if "git" in g]
    if not lists or not gits:
        fail("generator",
             "matrix must pair a list generator (environments) with a git "
             "generator (discovered services)")
        return []

    envs = lists[0]["list"]["elements"]
    git_paths = gits[0]["git"]["directories"]
    raw = yaml.dump(spec["template"], default_flow_style=False)
    patch_tpl = spec.get("templatePatch")
    render = go_template if go else fasttemplate
    env_ref = "{{.environment}}" if go else "{{environment}}"

    apps = []
    for env in envs:
        # The git generator path must be parameterised by environment,
        # not hardcoded to one of them.
        for entry in git_paths:
            if env_ref not in entry["path"]:
                fail("generator",
                     "git directory path %r is not parameterised by %s"
                     % (entry["path"], env_ref))
            ok()

        for service in discover(env["environment"]):
            params = dict(env)
            dirpath = "gitops/overlays/%s/%s" % (env["environment"], service)
            if go:
                # Under Go templating the git generator exposes `path` as an
                # object, not a bare string.
                params["path"] = {
                    "path": dirpath,
                    "basename": service,
                    "basenameNormalized": service,
                    "segments": dirpath.split("/"),
                }
            else:
                params["path"] = dirpath
                params["path.basename"] = service

            try:
                rendered = yaml.safe_load(render(raw, params))
            except KeyError as e:
                fail("template",
                     "%s/%s: template references %s which no generator "
                     "supplies (missingkey=error)" % (env["environment"], service, e))
                continue

            if patch_tpl:
                try:
                    patch = yaml.safe_load(render(patch_tpl, params))
                except KeyError as e:
                    fail("templatePatch",
                         "%s/%s: templatePatch references %s which no "
                         "generator supplies" % (env["environment"], service, e))
                    patch = None
                if patch:
                    merge(rendered, patch)

            apps.append((env["environment"], service, rendered))
    return apps


# Fields the Application CRD types as booleans. Templating a string into any
# of these is invalid, and "false" is additionally truthy to anything doing a
# non-empty check - the inverse of the intent.
BOOLEAN_FIELDS = [
    ("spec", "syncPolicy", "automated", "prune"),
    ("spec", "syncPolicy", "automated", "selfHeal"),
    ("spec", "syncPolicy", "automated", "allowEmpty"),
]


def dig(d, path):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def check_boolean_fields(appset):
    """Reject boolean fields carrying a templated or quoted string.

    This is the defect this phase exists to catch: Go templating substitutes
    into strings, so prune: "{{.prune}}" renders the *string* "false" into a
    field the CRD types as bool.
    """
    tpl = appset["spec"].get("template", {})
    for path in BOOLEAN_FIELDS:
        v = dig({"spec": tpl.get("spec", {})}, path)
        if v is None:
            ok()
            continue
        if isinstance(v, str):
            if "{{" in v:
                fail("boolean-template",
                     "%s is templated (%r). Go templating only substitutes "
                     "into strings; this renders a string into a boolean "
                     "field. Use templatePatch instead."
                     % (".".join(path), v))
            else:
                fail("boolean-template",
                     "%s is the string %r, not a boolean" % (".".join(path), v))
        elif not isinstance(v, bool):
            fail("boolean-template",
                 "%s is %r (%s), expected a boolean"
                 % (".".join(path), v, type(v).__name__))
        ok()


def check_template_syntax(appset, text):
    """When goTemplate is on, every expression must use the Go form."""
    go = appset["spec"].get("goTemplate", False)
    exprs = re.findall(r"\{\{[^}]*\}\}", text)
    for e in exprs:
        # Strip the braces, then the whitespace-trim markers, then any
        # whitespace they were hiding. Order matters: "{{- if x }}" leaves a
        # leading space if the dashes are removed before the second strip.
        body = e.strip("{}").strip()
        body = body.lstrip("-").rstrip("-").strip()
        if not body:
            continue
        # Control structures and functions are Go-only and always valid here.
        if re.match(r"^(if|else|end|range|with|define|template|block)", body):
            ok()
            continue
        if go:
            if not body.startswith("."):
                fail("legacy-syntax",
                     "%s uses legacy fasttemplate syntax; with goTemplate: "
                     "true every parameter needs a leading dot (e.g. "
                     "{{.environment}})" % e)
            if body in (".path", "{{.path}}"):
                fail("legacy-syntax",
                     "%s renders the path object, not a string; use "
                     "{{.path.path}} for the directory" % e)
        else:
            if body.startswith("."):
                fail("legacy-syntax",
                     "%s uses Go-template syntax but goTemplate is not "
                     "enabled" % e)
        ok()


def check_no_hardcoded_services(text):
    """No service name may appear in the ApplicationSet."""
    services = discover("dev")
    for svc in services:
        if svc in text:
            fail("hardcoded",
                 "service name %r appears literally in applicationset.yaml; "
                 "services must be discovered, not listed" % svc)
        ok()


def main():
    appset = load("applicationset.yaml")
    project = load("project.yaml")
    appset_text = (ARGOCD / "applicationset.yaml").read_text(encoding="utf-8")

    print("Offline validation of the Argo CD configuration\n")

    # ---- structural ------------------------------------------------
    if appset.get("kind") != "ApplicationSet":
        fail("kind", "applicationset.yaml is not an ApplicationSet")
    if project.get("kind") != "AppProject":
        fail("kind", "project.yaml is not an AppProject")
    ok(2)

    go = appset["spec"].get("goTemplate", False)
    print("  goTemplate:        %s" % go)
    print("  goTemplateOptions: %s"
          % (appset["spec"].get("goTemplateOptions") or "none"))
    print("  templatePatch:     %s\n"
          % ("present" if appset["spec"].get("templatePatch") else "absent"))

    check_no_hardcoded_services(appset_text)
    check_template_syntax(appset, appset_text)
    check_boolean_fields(appset)

    # ---- expand the matrix -----------------------------------------
    apps = expand(appset)
    envs = sorted({e for e, _, _ in apps})
    services = sorted({s for _, s, _ in apps})
    expected = len(envs) * len(services)

    print("Service discovery")
    print("  environments (explicit list): %s" % ", ".join(envs))
    print("  services (discovered):        %d" % len(services))
    for s in services:
        print("      %s" % s)
    print()
    print("Matrix expansion: %d environments x %d services = %d Applications"
          % (len(envs), len(services), expected))
    print("  generated: %d\n" % len(apps))

    if len(apps) != expected:
        fail("matrix", "expected %d Applications, generated %d"
             % (expected, len(apps)))
    ok()

    # every environment must discover the same service set
    per_env = {e: sorted(s for x, s, _ in apps if x == e) for e in envs}
    for e in envs:
        if per_env[e] != services:
            missing = set(services) - set(per_env[e])
            fail("matrix", "environment %s is missing overlays for %s"
                 % (e, sorted(missing)))
        ok()

    # ---- per-Application checks ------------------------------------
    allowed_dests = {(d["server"], d["namespace"]) for d in project["spec"]["destinations"]}
    allowed_repos = set(project["spec"]["sourceRepos"])
    names = set()

    for env, svc, app in apps:
        spec = app["spec"]
        name = app["metadata"]["name"]

        if name != "%s-%s" % (svc, env):
            fail("naming", "%s should be named %s-%s" % (name, svc, env))
        if name in names:
            fail("naming", "duplicate Application name %s" % name)
        names.add(name)
        ok(2)

        # unresolved template variables
        leftover = re.findall(r"\{\{[^}]+\}\}", yaml.dump(app))
        if leftover:
            fail("template", "%s has unresolved variables: %s"
                 % (name, sorted(set(leftover))))
        ok()

        # rendered booleans must be real booleans, not strings
        for bpath in BOOLEAN_FIELDS:
            v = dig(app, bpath)
            if v is not None and not isinstance(v, bool):
                fail("boolean-render",
                     "%s rendered %s as %r (%s), not a boolean"
                     % (name, ".".join(bpath), v, type(v).__name__))
            ok()

        # source path must be a real directory containing a kustomization.
        # Paths are repo-relative ("gitops/overlays/..."), so resolve them
        # against the repository root rather than this file's location.
        src_path = REPO / spec["source"]["path"]
        kustomization = src_path / "kustomization.yaml"
        if not kustomization.exists():
            fail("path", "%s points at %s which has no kustomization.yaml"
                 % (name, spec["source"]["path"]))
            ok()
            continue
        ok()

        # path must match the environment and service it claims
        want = "gitops/overlays/%s/%s" % (env, svc)
        if spec["source"]["path"] != want:
            fail("path", "%s path is %s, expected %s"
                 % (name, spec["source"]["path"], want))
        ok()

        # project membership
        if spec.get("project") != "boutique":
            fail("project", "%s is in project %r, expected 'boutique'"
                 % (name, spec.get("project")))
        ok()

        # repo must be allowed by the AppProject
        if spec["source"]["repoURL"] not in allowed_repos:
            fail("project", "%s uses repoURL %s which is not in the "
                 "AppProject sourceRepos" % (name, spec["source"]["repoURL"]))
        ok()

        # destination must be allowed by the AppProject
        dest = (spec["destination"]["server"], spec["destination"]["namespace"])
        if dest not in allowed_dests:
            fail("project", "%s targets %s which the AppProject does not "
                 "allow" % (name, dest))
        ok()

        # the Application namespace must match what the overlay renders
        overlay_ns = yaml.safe_load(
            kustomization.read_text(encoding="utf-8")
        ).get("namespace")
        if overlay_ns != spec["destination"]["namespace"]:
            fail("namespace", "%s destination namespace %r but the overlay "
                 "renders into %r" % (name, spec["destination"]["namespace"],
                                      overlay_ns))
        ok()

        # sync policy: automated everywhere, since the gate is Git
        automated = spec.get("syncPolicy", {}).get("automated")
        if not automated:
            fail("syncpolicy", "%s is not automated; the promotion gate "
                 "belongs in Git, not in a manual Argo sync" % name)
        else:
            if str(automated.get("selfHeal")).lower() != "true":
                fail("syncpolicy", "%s does not self-heal" % name)
            if env == "prod" and str(automated.get("prune")).lower() != "false":
                fail("syncpolicy", "prod Application %s has prune enabled" % name)
            if env != "prod" and str(automated.get("prune")).lower() != "true":
                fail("syncpolicy", "%s should prune in a non-prod environment"
                     % name)
        ok(3)

    # ---- AppProject shape -------------------------------------------
    if project["metadata"]["name"] != "boutique":
        fail("project", "AppProject must be named 'boutique'")
    if project["spec"].get("clusterResourceWhitelist") != []:
        fail("project", "AppProject should not permit cluster-scoped resources")
    kinds = {k["kind"] for k in project["spec"].get("namespaceResourceWhitelist", [])}
    if kinds != {"Deployment", "Service", "ServiceAccount"}:
        fail("project", "namespaceResourceWhitelist is %s; the overlays only "
             "render Deployment, Service and ServiceAccount" % sorted(kinds))
    ok(3)

    # ---- placeholders must be obvious, and consistent ---------------
    placeholder = "REPLACE-ME"
    appset_repos = {a[2]["spec"]["source"]["repoURL"] for a in apps}
    if len(appset_repos | allowed_repos) != 1:
        fail("placeholder", "repoURL differs between the ApplicationSet and "
             "the AppProject: %s vs %s" % (appset_repos, allowed_repos))
    ok()
    if not any(placeholder in r for r in appset_repos):
        print("  note: repoURL no longer contains %s - assuming it is real\n"
              % placeholder)

    # ---- report -----------------------------------------------------
    print("Sample generated Applications")
    print("-" * 72)
    for target in ("frontend-dev", "paymentservice-staging", "checkoutservice-prod"):
        app = next((a for _, _, a in apps if a["metadata"]["name"] == target), None)
        if not app:
            fail("sample", "expected Application %s was not generated" % target)
            continue
        s = app["spec"]
        print("  %s" % target)
        print("      path:      %s" % s["source"]["path"])
        print("      revision:  %s" % s["source"]["targetRevision"])
        print("      namespace: %s" % s["destination"]["namespace"])
        print("      server:    %s" % s["destination"]["server"])
        auto = s.get("syncPolicy", {}).get("automated")
        if auto:
            print("      automated: prune=%s selfHeal=%s"
                  % (auto.get("prune"), auto.get("selfHeal")))
        else:
            print("      automated: NONE (manual sync)")
    print()

    print("Checks run:      %d" % checks)
    print("Applications:    %d" % len(apps))
    print("Problems found:  %d\n" % len(problems))
    if problems:
        print("PROBLEMS")
        print("-" * 72)
        for cat, msg in problems:
            print("  [%s] %s" % (cat, msg))
        return 1
    print("PASS - ApplicationSet and AppProject are internally consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
