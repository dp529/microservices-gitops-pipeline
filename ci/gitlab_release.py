#!/usr/bin/env python
"""Use the existing digest writers in the explicitly configured GitOps repository."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import urlparse
import overlay
import publish
import gitlab_gitops

WORKTREE = None


def run(*args):
    if args[0] == "git":
        gitlab_gitops.git(list(args[1:]), cwd=WORKTREE, write=True)
    else:
        subprocess.run(args, check=True, cwd=WORKTREE)


def fresh_main(expected=None):
    global WORKTREE
    url, branch, _ = gitlab_gitops.target(os.environ, write=True)
    WORKTREE = Path(tempfile.mkdtemp(prefix="boutique-gitops-"))
    gitlab_gitops.git(["clone", "--branch", branch, "--single-branch", url, str(WORKTREE)], write=True)
    latest = gitlab_gitops.git(["rev-parse", "HEAD"], cwd=WORKTREE, write=True, capture=True)
    if expected and latest != expected:
        raise SystemExit("GitOps main advanced after detection; run a fresh pipeline. Refusing stale promotion.")
    overlay.REPO = WORKTREE
    return branch


def changed():
    # diff --quiet uses exit 1 for a normal difference, so do not use the
    # fail-on-error Git wrapper here.
    return bool(gitlab_gitops.git(["diff", "--name-only"], cwd=WORKTREE, write=True, capture=True))


def digest_records(directory, expected):
    records = {p.stem: json.loads(p.read_text()) for p in Path(directory).glob("*.json")}
    if set(records) != set(expected):
        raise SystemExit("Registry digest artifact set does not match all selected services")
    for record in records.values():
        overlay.validate_digest(record["digest"])
        if not record.get("tag", "").startswith("sha-"):
            raise SystemExit("Missing traceability tag")
    return records


def push(ref, options=()):
    # GIT_ASKPASS reads the appropriate secret from the masked CI environment;
    # the origin URL and command arguments contain no credential.
    gitlab_gitops.git(["push", *options, "origin", "HEAD:" + ref], cwd=WORKTREE, write=True)


def github_review(branch, base, count):
    env = os.environ
    repository = urlparse(env["GITOPS_REPO_URL"]).path.removesuffix(".git").strip("/")
    payload = {"title": "Promote to prod: %d service(s)" % count,
               "head": branch, "base": base,
               "body": "Copies immutable image digests from staging to prod without rebuilding. Merging this PR authorizes Argo CD reconciliation."}
    request = urllib.request.Request("https://api.github.com/repos/" + repository + "/pulls",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + env["GITHUB_GITOPS_TOKEN"],
                                              "Accept": "application/vnd.github+json",
                                              "Content-Type": "application/json",
                                              "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request) as response:
        print("Production review: " + json.load(response)["html_url"])


def commit(target, message):
    run("git", "config", "user.name", "GitLab CI")
    run("git", "config", "user.email", "gitlab-ci@users.noreply.gitlab.com")
    run("git", "add", "gitops/overlays/" + target)
    run("git", "commit", "-m", message)


def main():
    env = os.environ
    mode = sys.argv[1]
    if mode == "publish":
        configured = [bool(env.get(k)) for k in ("AWS_REGION", "AWS_ACCOUNT_ID", "AWS_ROLE_ARN")]
        if not any(configured):
            print("AWS unconfigured: no digest publication or GitOps promotion.")
            return
        if not all(configured):
            raise SystemExit("Incomplete AWS configuration")
        records = digest_records("digests", env["EXPECTED_SERVICES"].split())
        if not records:
            raise SystemExit("No registry digests found; refusing promotion")
        if not env.get("EXPECTED_GITOPS_SHA"):
            raise SystemExit("Missing GitOps baseline from detection")
        branch = fresh_main(env["EXPECTED_GITOPS_SHA"])
        registry = "%s.dkr.ecr.%s.amazonaws.com" % (env["AWS_ACCOUNT_ID"], env["AWS_REGION"])
        for service, record in sorted(records.items()):
            if publish.main(["--registry", registry, "--tag", record["tag"], service + "=" + record["digest"]]):
                raise SystemExit("digest publication failed")
        run(sys.executable, "ci/verify_release.py", "--expect-env", "dev")
        if changed():
            commit("dev", "dev: pin %d service digest(s) [skip ci]" % len(records))
            push(branch)
        return
    source, target = env["PROMOTE_SOURCE"], env["PROMOTE_TARGET"]
    if overlay.PROMOTION_PATH.get(source) != target:
        raise SystemExit("Invalid promotion direction")
    branch = fresh_main()
    names = sorted(p.name for p in (overlay.REPO / "gitops/overlays" / source).iterdir() if p.is_dir())
    requested = env.get("PROMOTE_SERVICES", "all")
    if requested == "all":
        names = [s for s in names if overlay.read_image(source, s)[1]
                 and overlay.read_image(source, s)[1] != overlay.read_image(target, s)[1]]
    else:
        names = requested.split()
    if not names:
        print("No digests differ; no promotion needed.")
        return
    run(sys.executable, "ci/overlay.py", "promote", source, target, *names)
    run(sys.executable, "ci/verify_release.py", "--expect-env", target, "--expect-services", *names)
    run(sys.executable, "ci/test_promotion.py", "--compare", source, target, *names)
    if target == "staging":
        commit(target, "staging: promote %d service(s) [skip ci]" % len(names))
        push(branch)
    else:
        review_branch = "promote/prod-" + env["CI_PIPELINE_ID"]
        run("git", "checkout", "-b", review_branch)
        commit(target, "prod: promote %d service(s) from staging" % len(names))
        if env["GITOPS_BACKEND"] == "github":
            push(review_branch)
            github_review(review_branch, branch, len(names))
        else:
            push(review_branch, ("-o", "merge_request.create", "-o", "merge_request.target=" + branch,
                                 "-o", "merge_request.title=Promote staged digests to prod"))
            print("Review the merge request; run its MR pipeline explicitly (job-token pushes trigger no pipeline).")


if __name__ == "__main__":
    main()
