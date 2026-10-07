#!/usr/bin/env python
"""Offline checks for GitLab boundaries and the adaptation of existing topology."""
import copy
import os
from pathlib import Path
import subprocess
import unittest
import tempfile
from unittest.mock import patch
import yaml
import affected
import gitlab_generate as generate
import gitlab_release as release
import gitlab_gitops as gitops
import render_gitlab_argocd as argo


class GitLabChecks(unittest.TestCase):
    def setUp(self):
        self.services = affected.load_config()[0]
        self.env = {"CI_COMMIT_SHA": "a" * 40, "CI_DEFAULT_BRANCH": "main",
                    "CI_COMMIT_BRANCH": "main", "CI_PIPELINE_SOURCE": "push",
                    "CI_COMMIT_BEFORE_SHA": "0" * 40}

    def test_initial_import_builds_all(self):
        self.assertEqual(len(generate.selection(self.env)), 10)

    def test_first_mr_uses_diff_base_not_all_services(self):
        self.env.update(CI_PIPELINE_SOURCE="merge_request_event", CI_MERGE_REQUEST_DIFF_BASE_SHA="b" * 40)
        with patch.object(affected, "changed_files", return_value=["src/cartservice/tests/test.cs"]) as diff:
            result = generate.selection(self.env)
        self.assertEqual([s["name"] for s in result], ["cartservice"])
        self.assertEqual(result[0]["context"], "src/cartservice/src")
        diff.assert_called_once_with("b" * 40, "a" * 40, None)

    def test_shared_proto_selects_ten(self):
        self.env["CI_COMMIT_BEFORE_SHA"] = "b" * 40
        with patch.object(affected, "changed_files", return_value=["protos/demo.proto"]):
            self.assertEqual(len(generate.selection(self.env)), 10)

    def test_unknown_service_fails(self):
        self.env["CI_COMMIT_BEFORE_SHA"] = "b" * 40
        with patch.object(affected, "changed_files", return_value=["src/unknown/main.go"]):
            with self.assertRaises(SystemExit):
                generate.selection(self.env)

    def test_docs_only_has_runnable_noop_child(self):
        self.env["CI_COMMIT_BEFORE_SHA"] = "b" * 40
        with patch.object(affected, "changed_files", return_value=["docs/note.md"]):
            cfg = generate.pipeline(generate.selection(self.env), True)
        self.assertIn("no-services-affected", cfg)
        self.assertNotIn("pin-dev", cfg)

    def test_mr_has_no_aws_id_token_or_release(self):
        cfg = generate.pipeline(self.services, False)
        self.assertNotIn("pin-dev", cfg)
        self.assertEqual(cfg["variables"]["PUBLISH_IMAGES"], "false")
        self.assertEqual(cfg["workflow"]["rules"][0]["if"], '$CI_PIPELINE_SOURCE == "parent_pipeline"')
        for name, job in cfg.items():
            if name.startswith("build-"):
                self.assertNotIn("id_tokens", job)
                self.assertEqual(job["rules"][0]["if"], '$CI_PIPELINE_SOURCE == "parent_pipeline"')

    def test_all_ten_artifacts_reach_dev(self):
        cfg = yaml.safe_load(yaml.safe_dump(generate.pipeline(self.services, True)))
        self.assertEqual(len(cfg["pin-dev"]["needs"]), 10)
        self.assertEqual(set(cfg["pin-dev"]["variables"]["EXPECTED_SERVICES"].split()),
                         {s["name"] for s in self.services})
        for service in self.services:
            job = cfg["build-" + service["name"]]
            self.assertEqual(job["variables"]["DOCKERFILE"], service["dockerfile"])
            self.assertEqual(job["id_tokens"]["AWS_ID_TOKEN"]["aud"], "sts.amazonaws.com")
            self.assertIn({"job": "build-" + service["name"], "artifacts": True}, cfg["pin-dev"]["needs"])

    def test_partial_digest_artifacts_fail_before_any_git_write(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "frontend.json").write_text('{"tag":"sha-test-job-1","digest":"sha256:' + 'a' * 64 + '"}')
            with self.assertRaises(SystemExit):
                release.digest_records(directory, ["frontend", "cartservice"])
            self.assertEqual(set(release.digest_records(directory, ["frontend"])), {"frontend"})

    def test_stale_pipeline_cannot_checkout_or_write(self):
        with patch.object(gitops, "target", return_value=("https://example.invalid/repo.git", "main", {})), \
                patch.object(gitops, "git", side_effect=[None, "c" * 40]) as git, \
                tempfile.TemporaryDirectory() as directory, patch.object(tempfile, "mkdtemp", return_value=directory):
            with self.assertRaises(SystemExit):
                release.fresh_main("a" * 40)
        self.assertEqual(git.call_count, 2)
        self.assertFalse(any("push" in call.args[0] for call in git.call_args_list))

    def test_unconfigured_aws_cannot_promote(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["release", "publish"]), \
                patch.object(release, "fresh_main") as fresh:
            release.main()
        fresh.assert_not_called()

    def test_failed_push_does_not_expose_token_in_exception(self):
        env = dict(CI_JOB_TOKEN="test-secret", CI_PROJECT_URL="https://gitlab.invalid/group/repo",
                   GITOPS_BACKEND="gitlab", GITOPS_REPO_URL="https://gitlab.invalid/group/repo.git", GITOPS_BRANCH="main")
        with patch.dict(os.environ, env), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 128)):
            with self.assertRaises(SystemExit) as error:
                release.push("main")
        self.assertNotIn("test-secret", str(error.exception))

    def test_job_token_cannot_push_to_another_gitlab_project(self):
        env = dict(CI_PROJECT_URL="https://gitlab.invalid/group/repo", GITOPS_BACKEND="gitlab",
                   GITOPS_REPO_URL="https://gitlab.invalid/group/other.git", GITOPS_BRANCH="main")
        with self.assertRaises(SystemExit):
            gitops.target(env, write=True)

    def test_github_requires_a_separate_write_credential(self):
        env = dict(CI_JOB_TOKEN="does-not-grant-github-access", GITOPS_BACKEND="github",
                   GITOPS_REPO_URL="https://github.com/dp529/microservices-gitops-pipeline.git", GITOPS_BRANCH="main")
        with self.assertRaises(SystemExit):
            gitops.target(env, write=True)
        env["GITHUB_GITOPS_TOKEN"] = "test-secret"
        url, _, git_env = gitops.target(env, write=True)
        self.assertNotIn("test-secret", url)
        self.assertNotIn("test-secret", git_env["GIT_ASKPASS"])

    def argo_config(self):
        return {"repoURL": "https://gitlab.example.invalid/group/boutique.git", "targetRevision": "main",
                "destinations": {e: {"server": "https://cluster.example.invalid", "namespace": "boutique-" + e}
                                 for e in ("dev", "staging", "prod")}}

    def test_all_argo_sources_and_destinations_match(self):
        config = self.argo_config()
        appset, project = argo.render(config)
        generators = appset["spec"]["generators"][0]["matrix"]["generators"]
        self.assertEqual(generators[1]["git"]["repoURL"], config["repoURL"])
        self.assertEqual(appset["spec"]["template"]["spec"]["source"]["repoURL"], config["repoURL"])
        self.assertEqual(project["spec"]["sourceRepos"], [config["repoURL"]])
        for item in generators[0]["list"]["elements"]:
            self.assertEqual(item["destinationServer"], config["destinations"][item["environment"]]["server"])
        self.assertTrue(appset["spec"]["syncPolicy"]["preserveResourcesOnDeletion"])
        self.assertEqual(project["spec"]["clusterResourceWhitelist"], [])

    def test_unresolved_argo_target_rejected(self):
        config = yaml.safe_load((argo.ROOT / "ci/argocd-gitlab.example.yaml").read_text())
        with self.assertRaises(ValueError):
            argo.render(config)

    def test_wrong_namespace_rejected(self):
        config = self.argo_config()
        config["destinations"]["prod"]["namespace"] = "default"
        with self.assertRaises(ValueError):
            argo.render(config)

    def test_parent_waits_for_child_and_retains_prod_review_gate(self):
        cfg = yaml.safe_load((argo.ROOT / ".gitlab-ci.yml").read_text())
        self.assertEqual(cfg["delivery"]["trigger"]["strategy"], "mirror")
        self.assertEqual(cfg["promote-prod"]["environment"], "prod-promotion")
        self.assertEqual(cfg["promote-prod"]["rules"][0]["when"], "manual")
        self.assertFalse(cfg["promote-prod"]["allow_failure"])


if __name__ == "__main__":
    unittest.main()
