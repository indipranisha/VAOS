# Local TLS test fixture

`siem-loopback-server.pem` and `siem-loopback-key.pem` are test-only material for local transport tests. The certificate has a DNS subject alternative name for `logs.example.com` and a 2020–2099 validity window; receivers bind only to `127.0.0.1`. Four transport tests explicitly disable certificate verification in their loopback-only test client to exercise encrypted request framing and contents. A fifth keeps VAOS's default certificate-verifying context and confirms an untrusted test certificate is rejected before any HTTP payload is sent.

`siem-test-root-ca.pem`, `siem-trusted-server-chain.pem`, and `siem-trusted-server-key.pem` form a separate self-signed test CA/server identity. Two in-memory TLS tests verify that the default Python TLS client accepts the trusted matching hostname and rejects a mismatched hostname. They use `ssl.MemoryBIO`; no socket, external receiver, or network access is involved. These tests do not prove external SIEM interoperability. Production TLS code continues to use Python's default certificate-verifying context. Never use any of these test keys outside the tests.

Current documentation release: **1.3.3**. See the main documentation index and release knowledgebase for current product behavior. Historical validation records retain their original versions.
