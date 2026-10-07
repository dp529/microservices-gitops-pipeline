#!/usr/bin/env bash
set -euo pipefail
# Invoked inside a disposable Linux CI container, never on the EKS cluster.
if command -v apk >/dev/null; then
  apk add --no-cache bash curl git python3 py3-pip
else
  apt-get update -qq
  apt-get install -y -qq curl git ca-certificates
fi
python3 -m venv /tmp/boutique-ci-venv
/tmp/boutique-ci-venv/bin/pip install --quiet PyYAML==6.0.2 awscli==1.42.40
curl --fail --silent --show-error --location \
  "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /tmp/kubectl
curl --fail --silent --show-error --location \
  "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256" -o /tmp/kubectl.sha256
printf '%s  /tmp/kubectl\n' "$(cat /tmp/kubectl.sha256)" | sha256sum --check
chmod +x /tmp/kubectl
mv /tmp/kubectl /usr/local/bin/kubectl
