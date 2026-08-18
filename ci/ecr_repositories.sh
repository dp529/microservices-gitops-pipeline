#!/usr/bin/env bash
# Create one ECR repository per deployable service.
#
# Run once, by someone with AWS credentials, before the first CI push. CI
# itself does not create repositories: it only pushes, so the IAM role it
# assumes does not need ecr:CreateRepository.
#
#   AWS_REGION=eu-west-1 ./ci/ecr_repositories.sh
#   AWS_REGION=eu-west-1 DRY_RUN=1 ./ci/ecr_repositories.sh   # print only
#
# One repository per service rather than one shared repository, because it
# gives per-service lifecycle policies, per-service scan findings, and IAM
# scoping per service. A single repository with the service name in the tag
# gives up all three.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_REGION:?set AWS_REGION}"
DRY_RUN="${DRY_RUN:-}"
PREFIX="boutique"

services=$(python -c "
import yaml
for s in yaml.safe_load(open('ci/services.yaml'))['services']:
    print(s['name'])
")

for name in $services; do
  repo="$PREFIX/$name"
  if [ -n "$DRY_RUN" ]; then
    echo "would create ecr repository: $repo"
    continue
  fi

  if aws ecr describe-repositories --repository-names "$repo" \
       --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "exists   $repo"
  else
    aws ecr create-repository \
      --repository-name "$repo" \
      --region "$AWS_REGION" \
      --image-scanning-configuration scanOnPush=true \
      --image-tag-mutability IMMUTABLE \
      >/dev/null
    echo "created  $repo"
  fi

  # Expire untagged images so failed or superseded builds do not accumulate.
  # Tagged images are kept: a digest referenced by a prod overlay must not
  # be deleted out from under a running deployment.
  aws ecr put-lifecycle-policy \
    --repository-name "$repo" \
    --region "$AWS_REGION" \
    --lifecycle-policy-text '{
      "rules": [
        {
          "rulePriority": 1,
          "description": "expire untagged images after 14 days",
          "selection": {
            "tagStatus": "untagged",
            "countType": "sinceImagePushed",
            "countUnit": "days",
            "countNumber": 14
          },
          "action": {"type": "expire"}
        }
      ]
    }' >/dev/null
done

echo
echo "IMAGE_TAG_MUTABILITY is IMMUTABLE: a tag cannot be repointed at a"
echo "different image once pushed. Deployment is digest-pinned regardless."
