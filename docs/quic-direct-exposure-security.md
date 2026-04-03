# Exposing a QUIC/HTTP3 Server Directly to the Internet

Security, resilience, and operational concerns when running a Rust QUIC server on the public internet without a CDN or reverse proxy handling connection termination.

-----

## Context

Traditional HTTP/1.1 and HTTP/2 deployments typically place a proxy, CDN, or load balancer in front of the application server. That intermediary handles TLS termination, DDoS mitigation, connection management, and observability. With QUIC/HTTP3, TLS is integrated into the transport protocol itself and cannot be separated — a proxy either terminates the entire QUIC connection (duplicating work) or passes UDP packets through without inspection.

This creates a compelling case for exposing your QUIC server directly. But doing so means inheriting every responsibility the intermediary was handling. This document catalogs those responsibilities.

-----

## 1. Volumetric DDoS and UDP Flooding

### The Exposure

UDP is connectionless at the network layer. Anyone can send a UDP packet to your port 443 with any source IP. Unlike TCP, there is no handshake that proves the sender can receive responses at their claimed address before your server commits resources. Your server will receive, read, and begin processing every packet that arrives.

### What a Proxy Normally Does

CDNs like Cloudflare absorb volumetric floods across their global network before traffic reaches your origin. They maintain massive bandwidth capacity specifically for this purpose and use anycast routing to distribute attack traffic across hundreds of points of presence.

### What You Need to Implement

**Layer 1 — Kernel/driver-level rate limiting.** If using AF_XDP, the XDP eBPF program can enforce per-source-IP packet rate limits before packets reach userspace. BPF hash maps track per-IP counters, and packets exceeding thresholds are dropped at the driver level with `XDP_DROP`. This is nanoseconds of work per dropped packet.

**Layer 2 — QUIC retry tokens for address validation.** Before allocating any connection state, require clients to complete a retry handshake. The server sends a retry packet containing an encrypted token bound to the client's IP address. The client must echo this token in its next initial packet. This proves the client can actually receive traffic at their claimed source address, defeating spoofed-source floods at the protocol level.

**Layer 3 — Connection-level budgets.** Once a connection is established, enforce memory and CPU budgets per connection. If a single connection is consuming disproportionate resources, terminate it. This catches slowloris-style attacks that pass the initial handshake but then abuse the connection.

### QUIC-Specific Nuance

QUIC initial packets (the very first packet from a client) are at least 1200 bytes due to the protocol's anti-amplification padding requirement. This means an attacker generating initial packets is spending at least 1200 bytes per packet, which raises the cost of flooding compared to minimal-size UDP packets. However, your server still has to read and validate each one.

-----

## 2. Stream Exhaustion

### The Exposure

A single QUIC connection can multiplex many concurrent streams. Each active stream consumes server-side resources: data buffers for partially received data, flow control window state, a unique stream ID entry, retransmission timers for any sent data, and application-level state (the HTTP/3 request being processed).

An attacker establishes a legitimate QUIC connection (passing retry validation and the full TLS handshake) and then opens streams as fast as the server allows, sending minimal data on each — just enough to keep them alive. The goal is to exhaust your server's memory or processing capacity through connection-internal resource consumption.

### What a Proxy Normally Does

CDNs and reverse proxies typically limit concurrent streams at their edge, process requests through their own HTTP/3 stack, and only forward well-formed, complete requests to your backend. The stream management burden stays at the edge.

### What You Need to Implement

**`MAX_STREAMS` transport parameter.** Set this conservatively in your QUIC handshake. The value should reflect your server's actual capacity, not a generous default. For an API server where clients typically have a few concurrent requests, a value of 100 is generous. Some QUIC implementations default to 256 or higher.

**Per-stream idle timeout.** If a stream receives no data for a configurable period, reset it. The QUIC protocol allows sending `STOP_SENDING` and `RESET_STREAM` frames to tear down individual streams without closing the connection.

**Per-connection memory ceiling.** Track the total memory allocated to all streams on a given connection. If it exceeds a threshold, close the connection with a `CONNECTION_CLOSE` frame using the `FLOW_CONTROL_ERROR` error code.

**Stream creation rate limiting.** Even within the `MAX_STREAMS` budget, an attacker can open and close streams rapidly — creating churn that costs CPU. Track the rate of stream creation per connection and throttle or close connections that exceed a reasonable rate.

### Detection Signals

- High stream-open-to-close ratio without corresponding response data
- Connections with many active streams but low data throughput
- Streams that remain open at the flow control limit without consuming data

-----

## 3. Zero-RTT Replay Attacks

### The Exposure

QUIC's 0-RTT feature allows a returning client to send application data in the very first packet of a new connection, using cryptographic material cached from a previous session. This eliminates one round trip and is excellent for latency. However, the data sent in 0-RTT packets is inherently replayable.

An attacker who captures a 0-RTT packet (from network observation, a compromised intermediary, or any point along the path) can resend that exact packet to your server. The server will decrypt it successfully (using the same session ticket) and process the contained request. If that request has side effects — creating an order, transferring funds, incrementing a counter, sending a message — the side effect is duplicated.

The TLS 1.3 specification acknowledges this and explicitly states that 0-RTT data does not have replay protection.

### What a Proxy Normally Does

CDNs typically accept 0-RTT data only for safe HTTP methods (GET, HEAD) and reject or defer it for state-changing methods. Some maintain server-side replay caches that track recently seen 0-RTT tokens.

### What You Need to Implement

**Option A — Disable 0-RTT entirely.** Safest. Costs one round trip on reconnection. For API servers where clients are typically long-lived and reconnection is infrequent, this may be the right default.

**Option B — Restrict 0-RTT to safe methods.** Accept 0-RTT data but only process GET and HEAD requests from it. If the 0-RTT data contains a POST/PUT/PATCH/DELETE, defer processing until the handshake completes (converting it to a 1-RTT request effectively). This requires your HTTP/3 layer to inspect the request method before committing to processing.

**Option C — Application-level idempotency enforcement.** Accept 0-RTT for all methods but require an idempotency key header (e.g., `Idempotency-Key: <uuid>`) on all state-changing requests. Your application maintains a cache of recently processed idempotency keys and rejects duplicates. This is the most flexible but pushes complexity to your API design.

**Option D — Server-side replay cache.** Maintain a time-limited cache of 0-RTT session ticket nonces. Reject any 0-RTT data that presents a previously seen nonce. This is what TLS 1.3 suggests as one mitigation, but it requires shared state across server instances (problematic in horizontally scaled deployments) and still has a time window where replays are possible.

### Implementation Consideration

Your QUIC library should expose a clear API for controlling 0-RTT acceptance policy. The default should be conservative (Option A or B). The decision of whether to accept 0-RTT should be hookable by the application, not buried in the transport layer.

```rust
enum ZeroRttPolicy {
    Disabled,
    SafeMethodsOnly,
    AllWithIdempotencyKey,
    Custom(Box<dyn Fn(&Request) -> ZeroRttDecision>),
}
```

-----

## 4. Connection Migration Abuse

### The Exposure

QUIC connections are identified by connection IDs, not by the IP/port 4-tuple. This allows a client to change its source IP address (e.g., switching from WiFi to cellular) without re-establishing the connection. The server recognizes the client by the connection ID in the packet header, not by where the packet came from.

An attacker can exploit this by sending packets with a valid connection ID from a spoofed source address. If the server accepts the migration without validation, it may start sending response data to the spoofed address (enabling reflection attacks) or waste resources processing packets from an address the legitimate client never moved to.

### What a Proxy Normally Does

CDNs handle connection migration at their edge. The migration only affects the client-to-CDN leg. The CDN-to-origin connection is separate and typically stable.

### What You Need to Implement

**Path validation (required by the QUIC RFC).** When a packet arrives from a new source address for an existing connection, do NOT immediately migrate. Instead, send a `PATH_CHALLENGE` frame to the new address containing a random token. Only migrate the connection if the client responds with a matching `PATH_RESPONSE` from the new address. This proves the client can send AND receive at the claimed address.

**Simultaneous path limit.** Limit the number of pending path validations per connection. An attacker sending migration attempts from thousands of spoofed IPs forces your server to maintain pending validation state for each. Cap this at a small number (2–4) and drop excess path challenges.

**Rate limit on migration attempts.** A legitimate client changes IP addresses infrequently — a few times per connection at most. If a connection is generating dozens of migration events per minute, it's either under attack or misconfigured. Either way, closing the connection is reasonable.

**Anti-amplification during migration.** Until the new path is validated, do not send more than three times the data received on the new path. This is the same anti-amplification principle as initial connection establishment and prevents your server from being used as a traffic amplifier toward the spoofed address.

-----

## 5. Amplification Attacks

### The Exposure

A UDP server that responds to unauthenticated packets with larger responses can be used as an amplification reflector. The attacker sends a small packet with a spoofed source IP (the victim's IP). The server sends a larger response to the victim. If the response is 10x the request size, the attacker has amplified their bandwidth 10x.

DNS amplification attacks use this principle with DNS servers that return large responses to small queries. QUIC is designed to resist this but the implementation must enforce the rules.

### What a Proxy Normally Does

CDNs absorb reflected traffic on behalf of their customers and implement protocol-level mitigations at their edge.

### What You Need to Implement

**The 3x anti-amplification limit (mandatory per RFC 9000, Section 8.1).** A server MUST NOT send more than three times the number of bytes received from an unvalidated address. This applies to initial connection establishment (before the handshake completes) and to connection migration (before path validation completes).

In practice, this means:

- A client sends a 1200-byte initial packet
- Your server may send up to 3600 bytes in response before receiving further data from the client
- If the server's initial response (ServerHello, certificates, etc.) exceeds 3600 bytes, it must wait for the client's next packet before sending more

**Retry tokens as address validation.** Using retry tokens (as described in the DDoS section) also serves the anti-amplification purpose. A client that completes the retry exchange has proven address ownership, and the 3x limit is lifted.

**Minimum initial packet size enforcement.** QUIC requires initial packets to be at least 1200 bytes (padded if necessary). Reject any initial packet smaller than this. An attacker trying to get amplification wants to send the smallest possible trigger packet — the 1200-byte minimum raises their cost.

### Testing

Your implementation should be tested with a tool that sends initial packets from spoofed source addresses and verifies that the server never sends more than 3x the received bytes to the unvalidated address. This is a concrete, measurable invariant.

-----

## 6. Certificate Management

### The Exposure

QUIC mandates TLS 1.3, which requires a valid X.509 certificate. Without a proxy handling certificate lifecycle, your server is directly responsible for acquisition, renewal, OCSP stapling, and hot-reloading of certificates.

### What a Proxy Normally Does

CDNs and reverse proxies handle Let's Encrypt ACME challenges, certificate renewal, OCSP stapling, and seamless certificate rotation. Your backend often runs with self-signed certs or no TLS at all on the internal leg.

### What You Need to Implement

**ACME client integration.** Your server (or a sidecar process) needs to obtain and renew certificates automatically. For a directly exposed server, the ACME HTTP-01 challenge requires serving a token on port 80 over HTTP, which means you need a minimal HTTP/1.1 listener just for certificate challenges. Alternatively, use DNS-01 challenges if you control the DNS records programmatically.

**Hot-reload without connection drops.** When a certificate is renewed, existing QUIC connections should continue using the old certificate until they naturally close. New connections should use the new certificate. Your TLS configuration needs to be swappable at runtime — typically by wrapping the certificate resolver behind an `Arc<RwLock<>>` or similar concurrent access pattern.

**OCSP stapling.** Clients may request OCSP status during the TLS handshake. Your server should periodically fetch OCSP responses from the certificate authority and staple them to the handshake. This avoids the client having to make a separate OCSP request, which adds latency and leaks browsing information.

**Certificate transparency.** Modern browsers expect certificates to include Signed Certificate Timestamps (SCTs). Let's Encrypt includes these automatically, but if you're using a different CA, verify that SCTs are present.

### Operational Consideration

Certificate renewal failure should trigger alerting. An expired certificate on a directly exposed QUIC server means total service outage — clients cannot connect at all. There is no fallback to unencrypted communication.

-----

## 7. Observability and Debugging

### The Exposure

QUIC encrypts almost everything. Unlike TCP+TLS where the TCP headers are in the clear (allowing network taps to see connection state, retransmissions, window sizes), QUIC encrypts its headers after the initial handshake. Even the connection ID can rotate. Network monitoring tools, IDS/IPS systems, and traditional packet capture workflows have severely reduced visibility into QUIC traffic.

When something goes wrong in production — elevated latency, connection failures, mysterious resets — your standard debugging toolkit is largely blind.

### What a Proxy Normally Does

CDNs provide dashboards, analytics, and logging for all traffic they terminate. They can see inside the QUIC connections because they're an endpoint. They expose metrics on connection duration, stream counts, error rates, and protocol-level events.

### What You Need to Implement

**qlog (QUIC event logging).** qlog is the IETF standard structured logging format for QUIC events (draft-ietf-quic-qlog-main-schema). It captures connection lifecycle events, packet sent/received events, stream state changes, congestion control decisions, and loss detection events. Your QUIC library should emit qlog traces that can be analyzed offline or streamed to a logging pipeline.

The qlog format is JSON-based (or CBOR for efficiency) and can be visualized with tools like qvis.

**SSLKEYLOGFILE support.** When debugging, your server should optionally write TLS session keys to a keylog file in the standard `SSLKEYLOGFILE` format. This allows decryption of captured QUIC packets in Wireshark for offline analysis. Obviously this must be disabled in production and restricted to debugging contexts.

**Application-level metrics.** Since network-level metrics are largely opaque, you need to export protocol-level metrics from within your application:

- Active connections (total, per source subnet)
- Active streams (total, per connection distribution)
- Handshake success/failure rates (distinguishing initial vs retry vs 0-RTT)
- Connection migration events (attempted, validated, failed)
- Packet loss rate (from QUIC's loss detection, not from network observation)
- Congestion window evolution
- 0-RTT acceptance/rejection rates
- Stream reset rates
- Memory consumption per connection (buffers, stream state)

These should be exposed via a Prometheus-compatible metrics endpoint, ideally on a separate management port that runs over HTTP/2 or HTTP/1.1 (so your monitoring infrastructure doesn't need QUIC support).

**Structured access logging.** Every request should produce a log entry that includes the QUIC connection ID, whether the connection was migrated, whether 0-RTT was used, the stream ID, and standard HTTP fields. When debugging an issue, you need to correlate application behavior back to the specific QUIC connection and stream.

**Health check endpoint.** Load balancers and orchestrators (Kubernetes liveness/readiness probes) typically health-check over HTTP/1.1 or TCP. Your QUIC server should expose a health endpoint on a separate TCP port for compatibility with existing infrastructure.

-----

## 8. Connection ID Design

### The Exposure

QUIC connection IDs are chosen by the server and included in every packet the client sends. They serve as the primary routing key — when a packet arrives, your server looks up the connection by its ID. In a multi-server deployment with a layer-4 load balancer, the load balancer also uses the connection ID to route packets to the correct backend server.

A poorly designed connection ID scheme creates operational and security problems.

### What You Need to Implement

**Structured connection IDs.** Encode a server identifier into a prefix of the connection ID so that a layer-4 load balancer can route packets to the correct backend without maintaining a connection table. AWS NLB's QUIC support uses exactly this approach — the server encodes its server ID into the connection ID and the NLB reads it for routing.

**Connection ID rotation.** QUIC allows both endpoints to issue new connection IDs during the lifetime of a connection. This is a privacy feature (preventing network observers from correlating packets across IP changes) but also a security feature — if an attacker learns a connection ID, the server can rotate to a new one. Your implementation should proactively rotate connection IDs on a configurable schedule.

**Unpredictable connection IDs.** Connection IDs must not be guessable. An attacker who can predict the connection ID of another user's connection can inject packets into it (which will fail cryptographic verification, but still consume server CPU to process and reject). Use a cryptographically random component in your connection ID generation.

**Connection ID length consistency.** Use a fixed connection ID length. Variable-length connection IDs can leak information about server state and complicate load balancer routing.

-----

## Responsibility Checklist

A summary of what you own when you remove the intermediary:

| Concern | With CDN/Proxy | Direct Exposure |
|---|---|---|
| Volumetric DDoS absorption | CDN's global network | Your server + XDP filtering |
| Address validation | CDN's edge | QUIC retry tokens (your impl) |
| Stream resource limits | CDN's HTTP/3 stack | Your QUIC transport config |
| 0-RTT replay safety | CDN policy | Your application logic |
| Connection migration validation | CDN's edge | Your QUIC path validation |
| Amplification prevention | CDN's protocol enforcement | Your 3x limit enforcement |
| Certificate lifecycle | CDN's managed TLS | Your ACME client + hot reload |
| Traffic observability | CDN dashboard | Your qlog + metrics + logging |
| Connection ID routing | CDN's infrastructure | Your CID scheme + LB config |
| Protocol-level CVE patching | CDN vendor | Your dependency updates |

Each row is a responsibility that transfers from someone else's engineering team to yours. The tradeoff is full control over the stack, lower latency (no extra hop), and no dependency on a third-party provider. Whether that tradeoff makes sense depends on your traffic profile, your team's capacity, and your tolerance for operational complexity.
