# GitLab CI with Argo CD delivery

This is a GitLab adaptation of the existing Online Boutique pipeline. Application
code, all ten Dockerfiles, the service registry, all thirty Kustomize overlays,
the original Argo CD manifests, and both GitHub Actions workflows are preserved.
It is prepared for review; no GitLab pipeline or cluster deployment has been run.

## Delivery path

```text
MR or main change -> existing validation -> affected-service detector
 -> one container build + Trivy scan per selected service
 -> main only: GitLab OIDC -> AWS STS -> ECR push -> registry digest
 -> collect every selected digest -> GitOps dev overlay commit
 -> Argo CD observes GitOps main -> reconciles workloads

manual dev -> staging: copy digests, commit GitOps main
manual staging -> prod: copy digests, create PR/MR, review and merge
 -> Argo CD reconciles the merged production references
```

CI uses `kubectl kustomize` only for offline rendering. It never runs
`kubectl apply`, Helm deployment commands, or Argo sync commands. A successful CI
job proves publication and Git updates, not workload health; check Argo CD Sync
and Health, readiness, dependencies and application requests after reconciliation.

`ci/services.yaml` remains the source of truth. `protos/` changes select all ten;
cartservice still builds from `src/cartservice/src` while watching all of
`src/cartservice`. Documentation/GitOps-only changes build no application image.
Initial import builds all services. For an explicit bootstrap or recovery run,
start a web pipeline on the default branch with `FORCE_BUILD_ALL=true`.

Trivy scans the built image before AWS authentication or pushing. The previous
GitLab lab policy is retained: HIGH/CRITICAL findings with fixes fail the job
(`--ignore-unfixed`, exit 1); vulnerability and secret scanners run. Reports are
retained as artifacts. The source Dockerfiles are not patched to bypass findings.
These ten services have not yet passed this scan in a real runner.

Tags use `sha-<short-sha>-job-<job-id>` so a retry can push to an immutable ECR
repository. Deployments use the digest read back from ECR, never that tag. Every
selected service must supply a valid digest artifact before dev publication.
Publication aborts if GitOps main advanced after detection; start a fresh pipeline
with `FORCE_BUILD_ALL=true` when retrying such a stale release.

## Two repository identities

The CI source will be the GitLab project the user chooses. Argo CD currently
watches `https://github.com/dp529/microservices-gitops-pipeline.git`, revision
`main`, under `gitops/overlays/<environment>/<service>`. The default GitOps write
target retains that exact GitHub repository. The release job clones that repo,
updates only the intended overlays, verifies them and pushes there. Changes to
GitLab's overlay copy alone do not deploy while Argo CD watches GitHub.

The GitLab source may diverge from GitHub after GitOps commits; the next release
reads the GitHub GitOps state freshly. Import/mirror source history deliberately;
do not blindly force-push either repository or configure a pull mirror that
overwrites GitOps commits. GitHub remains the deployment source of truth in this
default configuration.

| GitLab project variable | Purpose |
|---|---|
| `AWS_REGION` | agreed ECR region |
| `AWS_ACCOUNT_ID` | agreed AWS account, no literal account committed |
| `AWS_ROLE_ARN` | role trusting this GitLab project's default branch |
| `GITOPS_BACKEND` | `github` by default; `gitlab` only after agreed source migration |
| `GITOPS_REPO_URL` | existing GitHub URL by default; explicit selected GitLab URL if migrated |
| `GITOPS_BRANCH` | `main` by default; must match Argo targetRevision |
| `GITHUB_GITOPS_TOKEN` | masked/protected, repository-scoped GitHub write credential, only for GitHub backend |

Configure the GitHub credential in GitLab settings; never put it into a file,
URL, log or chat. A fine-grained token needs **Contents: read/write** and
**Pull requests: read/write** for this one GitHub repository; an installation
token with those repository permissions may also be supplied by the runner's
credential provider. It must have permission to push dev/staging overlay commits
under the existing GitHub branch rules. Do not disable branch protections to make
CI work. If main requires PRs for every write, direct dev/staging commits need an
explicitly agreed policy adjustment or a PR-based adaptation first.

`CI_JOB_TOKEN` is a GitLab credential and cannot push GitHub. For an agreed
same-project GitLab GitOps migration, use `GITOPS_BACKEND=gitlab` and set
`GITOPS_REPO_URL` to the selected project's HTTPS `.git` URL. The helper rejects
another GitLab project's URL. Enable **Settings > CI/CD > Job token permissions >
Allow Git push requests to the repository**. The job's triggering user must have
push permission on protected main; no cross-project access is needed.

Dev/staging commits include `[skip ci]`. GitLab job-token pushes also trigger no
new pipelines. Production goes to a separate review branch, never directly to
main. For GitHub the job opens a PR using the scoped GitHub credential; for
GitLab it uses Git push options to create an MR. With the GitLab backend, start
the MR pipeline explicitly because job-token pushes do not trigger it. Require
review and a successful validation pipeline before merge; no auto-merge option
is used. The GitHub PR path uses the preserved GitHub workflow for review builds.

## AWS setup prerequisites, not applied by this change

Use the existing GitLab OIDC provider with audience `sts.amazonaws.com`; trust
must match the chosen project and default branch exactly:

```text
gitlab.com:aud = sts.amazonaws.com
gitlab.com:sub = project_path:<namespace>/<project>:ref_type:branch:ref:<default-branch>
```

The existing `GitLabRunnerECRPushRole` trusts `mdileep2211/tes` main and allows only
the `fastapi-sre-lab` ECR repository. It cannot publish these ten images as-is.
After the project/account choices, prepare an explicit scoped role/trust update
or a dedicated role. No IAM update is included or performed by this patch.

One immutable ECR repository is needed for each name in `ci/services.yaml` under
`boutique/`. Preserve `ci/ecr_repositories.sh`: an authorized operator creates
repositories and lifecycle policies once, with scan-on-push. CI does not create
repositories. The CI role needs:

- `ecr:GetAuthorizationToken` on `*`.
- `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`,
  `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage`,
  `ecr:BatchGetImage`, and `ecr:DescribeImages` on the ten exact repository ARNs.

EKS node image-pull permissions are separate: registry auth, layer checks,
`BatchGetImage` and `GetDownloadUrlForLayer` for the application repositories.
No EKS control-plane or Kubernetes write permission belongs in the CI role.

All three AWS variables absent means build/scan only with an explicit skipped
publication message. Partial AWS configuration fails. MR jobs receive no AWS ID
token and never publish. Protect the AWS/GitHub variables for main and use a
trusted Linux amd64 Docker executor that supports privileged Docker-in-Docker;
GitLab shared runner compute minutes/storage limits may constrain ten builds.
Parent pipelines wait for the dynamic child with `strategy: mirror`
(GitLab >=18.2; same-project token push is GA >=18.4).

## Promotion controls

Start a web pipeline on the default branch with `CI_MODE=promote`, then run the
blocking manual job. Set `PROMOTE_SOURCE=dev`, `PROMOTE_TARGET=staging` for staging;
use `staging` and `prod` for production. `PROMOTE_SERVICES=all` copies every
published digest that differs; an explicit space-separated list is also accepted.
The original tool rejects skipped/backward steps and unpublished digests.
Promotion never builds or pushes a container.

For parity with the original protected `prod-promotion` GitHub environment,
configure the GitLab `prod-promotion` protected environment with required
approvers, plus repository review/merge rules. Protected environments require
GitLab Premium/Ultimate. A manual job alone does not recreate required-reviewer
enforcement; if the destination is Free, agree an available review gate before
claiming equivalent production approval. This is a prerequisite, not a silently
weakened setting.

## Argo CD configuration and pending choices

The existing ApplicationSet/AppProject are unchanged: three environments, ten
services each, namespace `boutique-dev`, `boutique-staging`, `boutique-prod`,
automatic self-heal, dev/staging prune, no prod prune, preserved generated
resources on ApplicationSet deletion, and restricted namespaced resource kinds.
Their current destination `https://kubernetes.default.svc` means the cluster
hosting Argo CD; it is not proof that the user's selected EKS cluster is registered.

The user still needs to choose the GitLab project and Kubernetes destination.
Do not change Argo repoURL to GitLab merely because CI runs there. If source
migration or destination configuration is agreed, copy
`ci/argocd-gitlab.example.yaml`, fill exact URL/revision/registered API URLs, then:

```bash
python ci/render_gitlab_argocd.py --config /path/to/agreed-targets.yaml --output-dir /tmp/argocd-review
```

This produces separate reviewable ApplicationSet/AppProject files and rejects
unresolved inputs, namespace mismatches and output over the original manifests.
It applies nothing. For the retained GitHub source, put the existing GitHub URL
in the input rather than an unchosen GitLab destination. The renderer keeps the
original matrix path and synchronization policies while changing only agreed
repo/revision/destination fields.

All existing overlays still carry upstream placeholder image tags. Do not apply
the full auto-sync ApplicationSet expecting those thirty applications to wait
for promotion: placeholders can run immediately. Bootstrap must explicitly
address that policy (for example, reviewed digest pins in every enabled
environment before registration). `--require-digests` rejects rendering until
every application overlay is digest-pinned. Enabling only dev initially would
change the original three-environment generator and needs agreement. Cluster
capacity for 30 applications plus Redis replicas, Argo installation, repository
read access, registered destination identity and ECR pull permissions must also
be verified before bootstrap. No live Argo/EKS changes have been made.

## Validation and execution limits

Run the five original suites and `python ci/test_gitlab.py`. YAML/Bash checks and
these offline tests validate topology, token boundaries, complete digest
aggregation, stale-release rejection and explicit Argo targets. They do not
prove real Docker builds/scans, AWS authentication, ECR push, GitHub/GitLab
promotion credentials or Argo reconciliation. Validate the parent and generated
child with authenticated GitLab CI Lint after choosing/importing the project,
then run the initial pipeline and verify publication, commit, Argo Sync/Health
and application traffic separately.

Primary references:

- [GitLab AWS OIDC](https://docs.gitlab.com/ci/cloud_services/aws/)
- [Dynamic child pipelines](https://docs.gitlab.com/ci/pipelines/downstream_pipelines/)
- [Job-token repository push](https://docs.gitlab.com/ci/jobs/ci_job_token/#allow-git-push-requests-to-your-project-repository)
- [GitLab push options](https://docs.gitlab.com/topics/git/commit/#push-options-for-merge-requests)
- [Protected environments](https://docs.gitlab.com/ci/environments/protected_environments/)
- [GitHub fine-grained token permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
- [Trivy filtering](https://trivy.dev/docs/latest/configuration/filtering/)
