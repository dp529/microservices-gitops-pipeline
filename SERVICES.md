# Services — Quick Reference

Ten Online Boutique services. Full detail in [docs/deployable-units.md](docs/deployable-units.md).

| Service | Language | Build context | Container port | Service port | Probe |
|---|---|---|---|---|---|
| frontend | Go | `src/frontend` | 8080 | 80 | HTTP `/_healthz` |
| cartservice | C# | `src/cartservice/src` | 7070 | 7070 | gRPC |
| productcatalogservice | Go | `src/productcatalogservice` | 3550 | 3550 | gRPC |
| currencyservice | Node | `src/currencyservice` | 7000 | 7000 | gRPC |
| paymentservice | Node | `src/paymentservice` | 50051 | 50051 | gRPC |
| shippingservice | Go | `src/shippingservice` | 50051 | 50051 | gRPC |
| emailservice | Python | `src/emailservice` | 8080 | 5000 | gRPC |
| checkoutservice | Go | `src/checkoutservice` | 5050 | 5050 | gRPC |
| recommendationservice | Python | `src/recommendationservice` | 8080 | 8080 | gRPC |
| adservice | Java | `src/adservice` | 9555 | 9555 | gRPC |

## Three things to remember

1. **`protos/demo.proto` affects all 10.** Generated stubs are committed per service. A proto-only change must rebuild everything.
2. **cartservice watch path != build context.** Watch `src/cartservice/**`, build from `src/cartservice/src`.
3. **emailservice port asymmetry.** Container 8080, Service 5000. Consumers use `emailservice:5000`.

Plus: **cartservice needs `redis-cart:6379`** to become ready.
