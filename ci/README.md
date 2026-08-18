# Affected-service detection

Given a range of commits, decide which of the ten services CI would need to
rebuild. Phase 5 stops at the decision: nothing is built or pushed.

    python ci/affected.py --base origin/main --head HEAD
    python ci/affected.py --files src/paymentservice/charge.js --json

Exit 0 when a decision was made (an empty matrix is a valid decision).
Exit 2 when a path under `src/` belongs to no configured service.

## Source of truth

`ci/services.yaml` holds one entry per service with three separate fields:

| Field | Meaning |
|---|---|
| `watch` | a change anywhere beneath this path means the service changed |
| `context` | the directory passed to `docker build` |
| `dockerfile` | the Dockerfile to build with |

For nine services `watch` and `context` are the same path. They are separate
fields because of cartservice, where they genuinely differ: the solution file
and tests live at `src/cartservice`, but the Dockerfile and `.csproj` live at
`src/cartservice/src`. Watching only the build context would miss a change to
the tests; building from the watch path would not find the Dockerfile.

Two additional lists:

- `fanout` - paths that affect every service. Only `protos` qualifies: all ten
  services compile against `protos/demo.proto` and commit their generated
  stubs. Without this, a proto-only change matches no service directory and
  builds nothing.
- `ignore` - paths that never trigger an application build, including
  `gitops/`. Rebuilding an image because an overlay changed would invert the
  promotion model, where CI produces an image and Git promotes it.

## Decision order

For each changed path, first match wins:

1. under a `fanout` path -> every service is affected
2. under an `ignore` path -> nothing is affected
3. under a service's `watch` path -> that service is affected
4. otherwise under `src/` -> unknown deployable unit, reported, exit 2
5. otherwise -> not an application file, no build

Matching is segment-wise, so `src/cart` does not match `src/cartservice`.

Deletions and renames count as changes: the diff is taken with
`--no-renames`, so a file moved between two services selects both.

## Output

    {
      "services": [
        {
          "name": "paymentservice",
          "context": "src/paymentservice",
          "dockerfile": "src/paymentservice/Dockerfile"
        }
      ]
    }

The `{"services": [...]}` shape is consumed directly by a GitHub Actions
matrix, where each entry is available as `matrix.services.name`,
`matrix.services.context` and `matrix.services.dockerfile`.

## Tests

    python ci/test_affected.py

23 checks. Cases covering deletions, renames, cross-service moves and
multi-commit ranges run against a real throwaway git repository rather than a
simulation of `git diff`. Two further checks assert that every path in
`services.yaml` exists on disk and that no directory under `src/` is left
unconfigured, so the config cannot drift from the source tree unnoticed.
