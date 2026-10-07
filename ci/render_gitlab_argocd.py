#!/usr/bin/env python
"""Render the existing ApplicationSet/AppProject for explicit GitLab/cluster targets.

Writes reviewable files only. It never contacts Argo CD or Kubernetes.
"""
import argparse
from pathlib import Path
import yaml
import overlay

ROOT = Path(__file__).resolve().parents[1]


def render(config):
    repo = config["repoURL"]
    revision = config["targetRevision"]
    destinations = config["destinations"]
    if (not repo.startswith("https://") or not repo.endswith(".git")
            or "REPLACE_" in repo or not revision or "REPLACE_" in revision):
        raise ValueError("Supply an explicit HTTPS GitLab repoURL and revision")
    if set(destinations) != set(overlay.ENVIRONMENTS):
        raise ValueError("Provide explicit dev, staging and prod destinations")
    appset = yaml.safe_load((ROOT / "gitops/argocd/applicationset.yaml").read_text())
    project = yaml.safe_load((ROOT / "gitops/argocd/project.yaml").read_text())
    generators = appset["spec"]["generators"][0]["matrix"]["generators"]
    for item in generators[0]["list"]["elements"]:
        target = destinations[item["environment"]]
        if not target["server"].startswith("https://") or "REPLACE_" in target["server"]:
            raise ValueError("Supply registered cluster API URL for " + item["environment"])
        if target["namespace"] != item["namespace"]:
            raise ValueError("Namespace must match the existing environment overlay")
        item["destinationServer"] = target["server"]
        item["revision"] = revision
    generators[1]["git"]["repoURL"] = repo
    generators[1]["git"]["revision"] = revision
    appset["spec"]["template"]["spec"]["source"]["repoURL"] = repo
    project["spec"]["sourceRepos"] = [repo]
    # Server is the identity; a guessed Argo cluster name can prevent matching.
    project["spec"]["destinations"] = [
        {"server": destinations[e]["server"], "namespace": destinations[e]["namespace"]}
        for e in overlay.ENVIRONMENTS
    ]
    return appset, project


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--require-digests", action="store_true",
                    help="before bootstrap, reject any application overlay still using placeholder tags")
    args = ap.parse_args()
    config = yaml.safe_load(Path(args.config).read_text())
    appset, project = render(config)
    if args.require_digests:
        for env in overlay.ENVIRONMENTS:
            for directory in (ROOT / "gitops/overlays" / env).iterdir():
                if not directory.is_dir():
                    continue
                _, digest, _ = overlay.read_image(env, directory.name)
                overlay.validate_digest(digest or "")
    out = Path(args.output_dir)
    if out.resolve() == (ROOT / "gitops/argocd").resolve():
        raise SystemExit("Use a separate review directory; original Argo manifests are preserved")
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("applicationset.yaml", appset), ("project.yaml", project)):
        (out / name).write_text(yaml.safe_dump(data, sort_keys=False))
    print("Rendered review files only. No cluster or Argo CD changes applied.")


if __name__ == "__main__":
    main()
