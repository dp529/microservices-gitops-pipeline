# Deployable Units

Discovery record for the 10 Online Boutique services in this repository.

**Source:** `GoogleCloudPlatform/microservices-demo`, commit `34ffea9175946982c3088ed84994fe6019ad6e92` (2026-08-11), Apache-2.0.
The application source under `src/` and `protos/` is that upstream code, moved in unmodified. It was verified byte-identical (180 source files + 2 protos, 0 hash mismatches) before the temporary upstream clone was deleted. This repository holds exactly one copy of the application.

Not carried over: `loadgenerator`, `shoppingassistantservice` (out of scope), and all upstream deployment tooling (`kubernetes-manifests/`, `helm-chart/`, `kustomize/`, `terraform/`, `skaffold.yaml`, `cloudbuild.yaml`, `istio-manifests/`, `release/`). GitOps configuration is built from scratch under `gitops/`.

## The 10 services

| # | Service | Language / Runtime | Source dir | Build context | Runtime image | Container port | Service port | Probes |
|---|---|---|---|---|---|---|---|---|
| 1 | frontend | Go 1.26 | `src/frontend` | `src/frontend` | distroless/static | 8080 | 80 | HTTP `GET /_healthz` |
| 2 | cartservice | C# / .NET 10 | `src/cartservice` | `src/cartservice/src` | dotnet runtime-deps chiseled | 7070 | 7070 | gRPC :7070 |
| 3 | productcatalogservice | Go 1.26 | `src/productcatalogservice` | `src/productcatalogservice` | distroless/static | 3550 | 3550 | gRPC :3550 |
| 4 | currencyservice | Node 20 | `src/currencyservice` | `src/currencyservice` | alpine:3.24 + nodejs | 7000 | 7000 | gRPC :7000 |
| 5 | paymentservice | Node 20 | `src/paymentservice` | `src/paymentservice` | alpine:3.24 + nodejs | 50051 | 50051 | gRPC :50051 |
| 6 | shippingservice | Go 1.26 | `src/shippingservice` | `src/shippingservice` | distroless/static | 50051 | 50051 | gRPC :50051 |
| 7 | emailservice | Python 3.14 | `src/emailservice` | `src/emailservice` | python:3.14-alpine | 8080 | **5000** | gRPC :8080 |
| 8 | checkoutservice | Go 1.26 | `src/checkoutservice` | `src/checkoutservice` | distroless/static | 5050 | 5050 | gRPC :5050 |
| 9 | recommendationservice | Python 3.14 | `src/recommendationservice` | `src/recommendationservice` | python:3.14-alpine | 8080 | 8080 | gRPC :8080 |
| 10 | adservice | Java 24/25, Gradle | `src/adservice` | `src/adservice` | temurin:25-jre-alpine | 9555 | 9555 | gRPC :9555 |

Common runtime characteristics (all 10):

- Port is configured via the `PORT` env var, except cartservice (`ASPNETCORE_HTTP_PORTS`) and shippingservice (which also sets `APP_PORT`).
- Probes use native Kubernetes `grpc:` — no `grpc-health-probe` sidecar or binary is required.
- Pods run non-root (uid/gid 1000) with `readOnlyRootFilesystem: true`, all capabilities dropped, `allowPrivilegeEscalation: false`.
- `terminationGracePeriodSeconds: 5`.

## Real-world details that must not be lost

These three were discovered from the actual source and each one breaks a later phase if forgotten.

### 1. `protos/demo.proto` is a fan-out dependency

All 10 services compile against the single shared contract in `protos/`. Each service has a `genproto.sh` invoking `protoc` against `../../protos`. Generated stubs are **committed** into each service (`genproto/*.pb.go` for Go, `demo_pb2*.py` for Python).

**Consequence (Phase 5):** a commit touching only `protos/` must fan out to a rebuild of all 10 services. Naive path-based change detection sees no service directory modified and builds nothing.

### 2. cartservice: watch path is not the build context

cartservice is the only service where these differ:

- **Watch path:** `src/cartservice/**` (a change under `tests/` or the `.sln` is still a cartservice change)
- **Build context:** `src/cartservice/src` (where the `Dockerfile` and `.csproj` live)

Every other service builds from its own service root.

**Consequence (Phase 5):** change detection and build invocation need separate path inputs per service; they cannot be assumed identical.

### 3. emailservice: container port 8080, Service port 5000

The container listens on 8080 (`email_server.py` reads `PORT`, defaulting to `"8080"`). The Kubernetes Service exposes **5000** and targets 8080. Consumers address it as `emailservice:5000` — checkoutservice's `EMAIL_SERVICE_ADDR` is `emailservice:5000`.

**Consequence (Phase 2B):** the emailservice Service manifest needs `port: 5000, targetPort: 8080`. Getting this wrong breaks order confirmation silently — checkout fails only at the final step.

### 4. redis-cart is an infra dependency of cartservice

cartservice requires Redis at `redis-cart:6379` (`REDIS_ADDR`). It does not become ready without it. Upstream defines the redis Deployment and Service inside `cartservice.yaml`.

**Open decision for Phase 2B:** whether `redis-cart` becomes its own Kustomize base or stays bundled in the cartservice base. It is not one of the 10 application services, but it is required for cartservice to run.

## Runtime dependency graph

```
frontend       --> productcatalog, currency, cart, recommendation,
                   shipping, checkout, ad
checkoutservice--> productcatalog, shipping, payment, email, currency, cart
recommendation --> productcatalog
cartservice    --> redis-cart
productcatalog, currency, payment, shipping, email, ad --> (leaf)
```

Leaf services deploy independently. `frontend` and `checkoutservice` are the fan-in points and should roll out last in any ordered deployment.

## Service address map

The inter-service addresses baked into env vars. These are what the Kustomize bases must produce.

| Consumer | Env var | Value |
|---|---|---|
| frontend | `PRODUCT_CATALOG_SERVICE_ADDR` | `productcatalogservice:3550` |
| frontend | `CURRENCY_SERVICE_ADDR` | `currencyservice:7000` |
| frontend | `CART_SERVICE_ADDR` | `cartservice:7070` |
| frontend | `RECOMMENDATION_SERVICE_ADDR` | `recommendationservice:8080` |
| frontend | `SHIPPING_SERVICE_ADDR` | `shippingservice:50051` |
| frontend | `CHECKOUT_SERVICE_ADDR` | `checkoutservice:5050` |
| frontend | `AD_SERVICE_ADDR` | `adservice:9555` |
| checkoutservice | `PRODUCT_CATALOG_SERVICE_ADDR` | `productcatalogservice:3550` |
| checkoutservice | `SHIPPING_SERVICE_ADDR` | `shippingservice:50051` |
| checkoutservice | `PAYMENT_SERVICE_ADDR` | `paymentservice:50051` |
| checkoutservice | `EMAIL_SERVICE_ADDR` | `emailservice:5000` |
| checkoutservice | `CURRENCY_SERVICE_ADDR` | `currencyservice:7000` |
| checkoutservice | `CART_SERVICE_ADDR` | `cartservice:7070` |
| recommendationservice | `PRODUCT_CATALOG_SERVICE_ADDR` | `productcatalogservice:3550` |
| cartservice | `REDIS_ADDR` | `redis-cart:6379` |

## Known deviations

Things that are deliberately not production-grade in this lab, recorded so they
are not mistaken for finished work.

| Item | Current | Required before production | Where |
|---|---|---|---|
| redis-cart image | `redis:alpine` — a **mutable tag** | `redis@sha256:<digest>`. A mutable tag means two deploys of the same git commit can land different Redis versions, which breaks the "git is the source of truth" guarantee GitOps depends on. | `gitops/base/cartservice/redis-deployment.yaml` |
| redis-cart storage | `emptyDir` | A PersistentVolumeClaim. Cart contents are lost on pod restart. | same file |
| Application image tags | `<service>:<env>-placeholder` | Immutable digests written by CI (Phase 6) | `gitops/overlays/*/*/kustomization.yaml` |
| Image registry | bare names, no registry host | ECR repository prefix via `newName` (Phase 7) | overlays |
| frontend external access | ClusterIP only | Ingress or LoadBalancer (Phase 7) | `gitops/base/frontend/service.yaml` |

The application images are placeholders by design and are resolved by CI. The
redis tag is different: nothing downstream will replace it, so it stays mutable
until someone changes it by hand.
