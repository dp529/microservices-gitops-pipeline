#!/usr/bin/env bash
set -euo pipefail
mkdir -p digests scan
# Unique attempt suffix permits retries with immutable ECR tags. GitOps deploys
# the registry digest; the tag remains traceability only, as in the source repo.
tag="sha-${CI_COMMIT_SHORT_SHA}-job-${CI_JOB_ID}"
image="boutique/${SERVICE_NAME}:${tag}"
docker info >/dev/null
docker build --file "$DOCKERFILE" --tag "$image" "$BUILD_CONTEXT"

# Preserve the prior GitLab lab gate: fixed HIGH/CRITICAL findings fail CI.
# Scanner is versioned and its archive checked against the release checksum.
archive="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"
release="https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}"
curl -fsSL "$release/$archive" -o "/tmp/$archive"
curl -fsSL "$release/trivy_${TRIVY_VERSION}_checksums.txt" -o /tmp/trivy-checksums.txt
(cd /tmp; grep "  ${archive}$" trivy-checksums.txt | sha256sum --check)
tar -xzf "/tmp/$archive" -C /usr/local/bin trivy
trivy image --scanners vuln,secret --exit-code 1 --severity HIGH,CRITICAL \
  --ignore-unfixed --format json --output "scan/${SERVICE_NAME}.json" "$image"

if [ "$PUBLISH_IMAGES" != true ]; then
  echo "Merge request: build/scan only; no AWS authentication or publication."
  exit 0
fi
if [ -z "${AWS_REGION:-}" ] && [ -z "${AWS_ACCOUNT_ID:-}" ] && [ -z "${AWS_ROLE_ARN:-}" ]; then
  echo "AWS unconfigured: build/scan completed; no image pushed or digest pinned."
  exit 0
fi
: "${AWS_REGION:?required}" "${AWS_ACCOUNT_ID:?required}" "${AWS_ROLE_ARN:?required}"
: "${AWS_ID_TOKEN:?GitLab ID token required}"
# Do not log the ID token or temporary credentials.
credentials=$(aws sts assume-role-with-web-identity --role-arn "$AWS_ROLE_ARN" \
  --role-session-name "boutique-${CI_PROJECT_ID}-${CI_JOB_ID}" \
  --web-identity-token "$AWS_ID_TOKEN" --duration-seconds 3600 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN <<< "$credentials"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset credentials AWS_ID_TOKEN
aws sts get-caller-identity
registry="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry"
remote="$registry/boutique/$SERVICE_NAME:$tag"
docker tag "$image" "$remote"
docker push "$remote"
digest=$(aws ecr describe-images --region "$AWS_REGION" --repository-name "boutique/$SERVICE_NAME" \
  --image-ids "imageTag=$tag" --query 'imageDetails[0].imageDigest' --output text)
if ! [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "Registry did not return a valid digest" >&2; exit 1
fi
printf '%s=%s\n' "$SERVICE_NAME" "$digest" > "digests/${SERVICE_NAME}.txt"
printf '{"tag":"%s","digest":"%s"}\n' "$tag" "$digest" > "digests/${SERVICE_NAME}.json"
