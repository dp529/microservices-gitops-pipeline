# CI: detection, build, publish, promotion

The delivery pipeline. Source changes select services, selected services are
built and pushed, the resulting digests are pinned into dev, and promotion
copies those digests onward without rebuilding.

```
change -> detect -> build only affected -> push to ECR -> read digest
                                                              |
                                                     pin into dev overlay
                                                              |
                                            promote (copy) -> staging
                                                              |
                                     promote via PR (copy) -> prod
```

## Files

| File | Purpose |
|---|---|
| `services.yaml` | source of truth: name, watch path, build context, dockerfile |
| `affected.py` | changed paths -> the services CI must build, as a JSON matrix |
| `build_all.py` | build every service locally from its real context |
| `publish.py` | write published digests into the dev overlays |
| `overlay.py` | read/write an overlay's image reference; promotion rules |
| `verify_release.py` | assert a release change is confined and digest-pinned |
| `ecr_repositories.sh` | one-time ECR repository creation (run by a human) |
| `test_affected.py` | 23 detector tests |
| `test_promotion.py` | 29 pinning and promotion tests |
| `test_pipeline.py` | 25 end-to-end simulation checks |

## Immutability

The deployment source of truth is a digest, never a tag:

```yaml
images:
  - name: paymentservice
    newName: <account>.dkr.ecr.<region>.amazonaws.com/boutique/paymentservice
    digest: sha256:3f0a91c5...
    # tag sha-9e7aaac is for human traceability only
```

The digest is read back from the registry after the push, so it is what the
registry actually stored. A commit SHA tag is recorded as a comment for
traceability, but nothing deploys from it. `overlay.py` refuses to write
anything that is not `sha256:` followed by 64 hex characters, so a mutable
tag cannot become the deployment reference by accident.

## Promotion

Promotion copies an existing digest to the next environment. It never builds.

    python ci/overlay.py promote dev staging paymentservice
    python ci/overlay.py promote staging prod paymentservice

Rejected, with the reason stated:

| Attempt | Why it fails |
|---|---|
| `dev -> prod` | skips staging; prod would run something staging never ran |
| `staging -> dev` | backwards |
| `dev -> dev` | same environment |
| `prod -> anything` | end of the line |
| promoting a service dev never published | there is no digest to copy |

A multi-service release - a `protos/` change fans out to all ten - is
promoted as a set, so no service is silently left behind on an older digest.

## Environment boundaries

- **dev** is written only by CI, on a push to `main`. A pull request builds
  the image to prove it compiles but publishes nothing.
- **staging** is written only by promotion, committed straight to `main`.
- **prod** is written only by promotion, and only through a pull request
  raised by a job bound to the `prod-promotion` GitHub environment. Merging
  that pull request is the production approval.

Git is the gate. Once a change is merged, Argo CD reconciles it; there is no
second manual sync step.

## AWS

Authentication is GitHub OIDC to an IAM role. No static access keys exist in
this repository, and `test_promotion.py` fails the build if an access key or
a 12-digit ECR host is ever committed.

Three repository variables are required, and are not committed:

| Variable | Example |
|---|---|
| `AWS_REGION` | `eu-west-1` |
| `AWS_ACCOUNT_ID` | the 12-digit account id |
| `AWS_ROLE_ARN` | `arn:aws:iam::<account>:role/<role>` |

When they are absent the workflow still builds every affected image, and
skips only the push and the digest pinning. Nothing pretends to have
succeeded.

One ECR repository per service under a `boutique/` prefix, created by
`ecr_repositories.sh`. Separate repositories give per-service lifecycle
policies, scan findings and IAM scoping; a single shared repository with the
service in the tag gives up all three.
