#!/usr/bin/env python
"""Explicit GitOps repository/credential boundary; no secrets in command URLs."""
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse


def target(env, write=False):
    backend = env["GITOPS_BACKEND"]
    url = env["GITOPS_REPO_URL"]
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.path.endswith(".git"):
        raise SystemExit("Use a credential-free HTTPS GitOps repository URL ending .git")
    if backend == "github":
        if parsed.netloc != "github.com":
            raise SystemExit("GitHub backend requires github.com")
        if write and not env.get("GITHUB_GITOPS_TOKEN"):
            raise SystemExit("Set masked/protected GITHUB_GITOPS_TOKEN with repository-scoped write access")
    elif backend == "gitlab":
        if url.rstrip("/") != env["CI_PROJECT_URL"].rstrip("/") + ".git":
            raise SystemExit("CI_JOB_TOKEN writes support only the same GitLab project in this pipeline")
    else:
        raise SystemExit("GITOPS_BACKEND must be github or gitlab")
    git_env = dict(env)
    git_env["GIT_TERMINAL_PROMPT"] = "0"
    helper = Path(__file__).resolve().with_name("gitlab_git_askpass.sh")
    if os.name != "nt":
        helper.chmod(0o700)
    git_env["GIT_ASKPASS"] = str(helper)
    return url, env["GITOPS_BRANCH"], git_env


def git(args, cwd=None, write=False, capture=False):
    _, _, git_env = target(os.environ, write=write)
    result = subprocess.run(["git", "-c", "credential.helper=", *args], cwd=cwd, env=git_env,
                            capture_output=capture, text=True)
    if result.returncode:
        raise SystemExit("GitOps Git operation failed; check repository credentials, permissions and branch freshness")
    return result.stdout.strip() if capture else None


def baseline():
    url, branch, _ = target(os.environ)
    result = git(["ls-remote", url, "refs/heads/" + branch], capture=True)
    if not result:
        raise SystemExit("GitOps branch does not exist")
    return result.split()[0]
