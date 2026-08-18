# Online Boutique — CI/CD + GitOps Exercise

A GitOps delivery pipeline for the ten Google Online Boutique microservices, built from scratch. The application source is real, unmodified upstream code; everything around it — Kustomize, Argo CD, CI — is authored here.

## Repository layout

```
.
├── protos/      shared gRPC contract (demo.proto) — all 10 services compile against it
├── src/         the ten application services, one directory each
├── gitops/      deployment configuration
│   ├── base/        per-service Kustomize bases
│   ├── overlays/    dev / staging / prod
│   └── argocd/      ApplicationSet and AppProject
└── docs/        discovery record and build topology
```

| Directory | Purpose | Never contains |
|---|---|---|
| `protos/` | The single shared gRPC contract. Kept top-level so its blast radius is visible in a diff — a change here affects all ten services. | Generated code |
| `src/` | The one and only application source tree. Each service is independently buildable with its own Dockerfile and dependency manifest. | Kubernetes manifests, CI config |
| `gitops/` | All deployment configuration, split into bases, environment overlays, and Argo CD resources. | Application source, Dockerfiles |
| `docs/` | Discovery findings and build topology — the details that drive CI and overlay design. | Generated output |

## The model

One base per service, three thin environment overlays pointing at it. The same image digest is promoted across environments; only namespace, replicas, and resources differ.

```
                src/paymentservice
                        |
                        |  CI: build on change
                        v
             image @ sha256:ABC  (immutable)
                        |
                        v
             gitops/base/paymentservice
                        |
        +---------------+---------------+
        v               v               v
      DEV            STAGING           PROD
      ABC              ABC              ABC
```

Each overlay stays tiny — a reference to the base, a namespace, an image digest, a replica count.

## Status

The GitOps layer, the affected-service detector, the promotion tooling and all
ten container builds are implemented and tested locally: 10/10 images build
from the real upstream Dockerfiles, 30/30 overlays render, and 132 checks pass
across five suites.

The two GitHub Actions workflows have **not yet been executed against real
infrastructure.** Their YAML is validated and the Python they call is covered
by tests, but the parts that only exist inside a runner - the OIDC handshake
with AWS, the ECR push, reading the digest back from the registry, passing
digests between matrix jobs as artifacts, and the pull-request-based
production gate - have not run end to end. No AWS account is wired up, so
every digest exercised in the tests is synthetic and the overlays still carry
their placeholder tags.

What that means in practice: the delivery logic is proven, the CI wiring is
not. See [ci/README.md](ci/README.md) for the AWS setup required to close
that gap.

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Discovery of the real source | Done |
| 2A | Repository layout | Done |
| 2B | 10 Kustomize bases + dev/staging/prod overlays | Done |
| 3 | Render and validate the 30 overlays | Done |
| 4 | Argo CD ApplicationSet + AppProject | Done |
| 5 | CI changed-service detection and builds | Done |
| 6 | Digest promotion dev → staging → prod | Done |
| 7 | ECR / EKS integration | Workflows written, not yet run against AWS |

## Source provenance

Application code is from `GoogleCloudPlatform/microservices-demo` at commit `34ffea9175946982c3088ed84994fe6019ad6e92`, Apache-2.0. It was moved in unmodified and verified byte-identical; the temporary clone was then deleted. There is exactly one copy of the application in this repository.

Upstream's own deployment tooling (Helm chart, Skaffold, Terraform, kubernetes-manifests) was deliberately not carried over — the point of the exercise is to build that layer.

See [SERVICES.md](SERVICES.md) for the service table and [docs/deployable-units.md](docs/deployable-units.md) for full build topology.
