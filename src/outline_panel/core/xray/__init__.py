"""Xray-core as a second backend, behind `ports.node.NodePort`.

Split three ways so the risky part is small and testable on its own:

  * `proto`  — protobuf wire encoding for the five messages this needs
  * `grpc`   — gRPC framing over HTTP/2
  * `api`    — `XrayAPI`, the adapter the registry hands out

Deliberately no `grpcio` and no `protobuf`. Those bring a large binary wheel,
generated stubs checked into the tree, and a version coupling to Xray's own
`.proto` files — for five messages whose fields are strings, a uint32 and an
int64. What is here instead is ~120 lines against the protobuf spec, with
byte-level tests taken from the real definitions at
github.com/XTLS/Xray-core. The only new dependency is `h2`, three pure-Python
wheels, and it is an optional extra: a panel that never configures an Xray
server never imports any of this.
"""
