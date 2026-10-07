#!/usr/bin/env python
"""Translate the existing affected-service detector into a GitLab child pipeline."""
import json
import os
from pathlib import Path
import subprocess
import yaml
import affected
import gitlab_gitops

ZERO = "0" * 40


def selection(env):
    services, fanout, ignore = affected.load_config()
    head = env["CI_COMMIT_SHA"]
    if env.get("FORCE_BUILD_ALL") == "true":
        return affected.build_matrix({s["name"] for s in services}, services)
    if env["CI_PIPELINE_SOURCE"] == "merge_request_event":
        base = env["CI_MERGE_REQUEST_DIFF_BASE_SHA"]
    else:
        base = env.get("CI_COMMIT_BEFORE_SHA", ZERO)
        if not base or base == ZERO:
            # Initial import needs all images, including root commits.
            if env["CI_PIPELINE_SOURCE"] == "push":
                return affected.build_matrix({s["name"] for s in services}, services)
            base = head + "^"
    files = affected.changed_files(base, head, None)
    names, _, unknown, _ = affected.classify(files, services, fanout, ignore)
    if unknown:
        raise SystemExit("unknown deployable paths: " + ", ".join(unknown))
    return affected.build_matrix(names, services)


def pipeline(services, publish, baseline=None):
    cfg = {
        "workflow": {"rules": [{"if": '$CI_PIPELINE_SOURCE == "parent_pipeline"'}]},
        "stages": ["build", "publish"],
        "variables": {"PUBLISH_IMAGES": "true" if publish else "false"},
        "default": {
            "image": "docker:27.5.1-cli",
            "before_script": ["apk add --no-cache bash", "bash ci/gitlab_setup.sh",
                              'export PATH="/tmp/boutique-ci-venv/bin:$PATH"'],
        },
    }
    for service in services:
        name = service["name"]
        cfg["build-" + name] = {
            "rules": [{"if": '$CI_PIPELINE_SOURCE == "parent_pipeline"'}],
            "stage": "build",
            "services": [{"name": "docker:27.5.1-dind", "alias": "docker"}],
            "variables": {"DOCKER_HOST": "tcp://docker:2375", "DOCKER_TLS_CERTDIR": "",
                          "SERVICE_NAME": name, "BUILD_CONTEXT": service["context"],
                          "DOCKERFILE": service["dockerfile"]},
            "script": ["bash ci/gitlab_build.sh"],
            "artifacts": {"when": "always", "paths": ["digests/", "scan/"],
                          "expire_in": "1 day"},
        }
        # Merge-request jobs get no AWS ID token and never publish.
        if publish:
            cfg["build-" + name]["id_tokens"] = {"AWS_ID_TOKEN": {"aud": "sts.amazonaws.com"}}
    if not services:
        cfg["no-services-affected"] = {"stage": "build", "before_script": [],
                                       "rules": [{"if": '$CI_PIPELINE_SOURCE == "parent_pipeline"'}],
                                       "script": ["echo 'No application services affected.'"]}
    elif publish:
        cfg["pin-dev"] = {
            "rules": [{"if": '$CI_PIPELINE_SOURCE == "parent_pipeline"'}],
            "stage": "publish", "image": "python:3.12-bookworm",
            "before_script": ["bash ci/gitlab_setup.sh",
                              'export PATH="/tmp/boutique-ci-venv/bin:$PATH"'],
            "variables": {"EXPECTED_SERVICES": " ".join(s["name"] for s in services),
                          "EXPECTED_GITOPS_SHA": baseline or ""},
            "needs": [{"job": "build-" + s["name"], "artifacts": True} for s in services],
            "resource_group": "boutique-gitops-main",
            "script": ["python ci/gitlab_release.py publish"],
        }
    return cfg


def main():
    env = os.environ
    services = selection(env)
    publish = (env["CI_PIPELINE_SOURCE"] in ("push", "web")
               and env.get("CI_COMMIT_BRANCH") == env["CI_DEFAULT_BRANCH"])
    Path("affected-services.json").write_text(json.dumps(services, indent=2) + "\n")
    configured = all(env.get(k) for k in ("AWS_REGION", "AWS_ACCOUNT_ID", "AWS_ROLE_ARN"))
    baseline = gitlab_gitops.baseline() if publish and services and configured else None
    Path("generated-gitlab-ci.yml").write_text(yaml.safe_dump(pipeline(services, publish, baseline), sort_keys=False))
    print("Selected: " + (", ".join(s["name"] for s in services) or "none"))


if __name__ == "__main__":
    main()
