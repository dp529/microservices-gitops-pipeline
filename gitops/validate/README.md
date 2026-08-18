# Semantic validation

`kustomize build` proves the YAML parses. It does not prove the YAML means
anything. A Service can select pods that do not exist, a probe can target a
port nothing listens on, and `checkoutservice` can point at the wrong port on
`emailservice` — all while rendering perfectly.

`semantic_check.py` renders all 30 overlays with `kubectl kustomize` and asserts
the wiring is coherent. It contacts no cluster and needs no network.

## Run

    python gitops/validate/semantic_check.py

Exit 0 on pass, 1 with a defect list on failure.

## What it checks

| Check | Failure it catches |
|---|---|
| Deployment selector vs its own pod labels | Deployment owns no pods, rollout hangs |
| Service selector resolves to exactly one workload | Empty endpoints, or a Service cross-wired to the wrong workload |
| Service `targetPort` is a real `containerPort` | Traffic accepted then blackholed |
| Probe port is a declared `containerPort` | Pod never becomes Ready |
| Both probes present on every container | Silent failure to detect a hung process |
| Ports match Phase 1 discovery | Someone "fixes" the emailservice 5000/8080 asymmetry |
| `*_SERVICE_ADDR` / `REDIS_ADDR` resolve to a real Service on its real port | Connection refused between services |
| Namespace on every resource, matching the environment | Resources land in the wrong namespace or default |
| `runAsNonRoot`, non-zero `runAsUser`, no privilege escalation, read-only rootfs, all capabilities dropped | Container runs privileged or as root |
| CPU and memory requests *and* limits on every container | Unbounded workload, noisy-neighbour eviction |
| Limits are not below requests | Unschedulable pod |
| Replica count matches the environment | Prod silently running at dev scale |
| No `:latest`, no untagged images | Non-reproducible deploy |
| Non-dev environments differ from dev only in namespace, replicas, image tag and redis sizing | Prod inherits a dev-only setting |
| ServiceAccount referenced is created by the same overlay | Pod fails admission |

## Ground truth

`EXPECTED_PORTS` and `EXPECTED_ENV` encode what Phase 1 discovered in the real
upstream source. They are the reference the overlays are checked against, so a
drift in either direction is reported rather than silently accepted.

## Mutation testing

A validator that never fails proves nothing. Twelve deliberate defects were
injected into a scratch copy of `gitops/` and all twelve were caught, including
the two that matter most for this repository:

- a `labels`/`commonLabels` directive on the cartservice base, which stamps
  `app: cartservice` onto redis-cart and cross-wires the cart Service
- `EMAIL_SERVICE_ADDR` changed to `emailservice:8080`, the "obvious fix" that
  actually breaks checkout

The scratch copy was deleted afterwards; the mutations never touched this tree.

# ApplicationSet validation

`appset_check.py` validates the Argo CD configuration without a cluster. Argo
CD is not installed and nothing is registered, so instead of a schema check it
*simulates the matrix generator* against the real repository layout and checks
the Applications it would produce.

    python gitops/validate/appset_check.py

## What it checks

| Check | Failure it catches |
|---|---|
| Exactly one matrix generator, pairing a list with a git generator | The topology silently changed shape |
| Git generator path is parameterised by `{{environment}}` | Generator hardcoded to one environment |
| No service name appears literally in the ApplicationSet | Services listed by hand instead of discovered |
| Expansion yields environments x services Applications | A service exists in some environments but not others |
| Every environment discovers the same service set | Partial rollout of a new service |
| Application names are `<service>-<environment>` and unique | Name collisions across environments |
| No unresolved `{{...}}` remain after substitution | A template variable the generator never supplies |
| Each source path exists and holds a `kustomization.yaml` | Application points at nothing |
| Application namespace matches what the overlay renders | Deploying into an unintended namespace |
| repoURL is in the AppProject `sourceRepos` | Application blocked at sync time |
| Destination is in the AppProject `destinations` | Application blocked at sync time |
| Every Application is automated and self-heals | A manual sync gate reintroduced on top of the Git gate |
| Prod does not prune; non-prod does | Prod tears down workloads on an accidental Git deletion |
| AppProject permits no cluster-scoped resources | Overly broad permissions |

## Mutation testing

Nine defects were injected into a repository-shaped scratch copy; all nine were
caught. Two bugs in the validator itself were found and fixed this way: source
paths were resolved against the wrong root, and the sample-report code crashed
on a missing `automated` block before the findings could print. Both would have
made the tool report success on broken input.
