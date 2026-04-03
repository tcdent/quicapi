# UDP I/O Optimizations for a Rust QUIC Server

A progressive optimization guide for reducing kernel overhead in a containerized Rust QUIC/HTTP3 server, from standard sockets through `io_uring` to AF_XDP.

-----

## The Problem

Every UDP packet your QUIC server sends or receives crosses the kernel-userspace boundary. This involves a syscall (context switch), copying packet data between kernel and userspace buffers, traversal of the full kernel networking stack (netfilter, routing, socket buffer allocation), and scheduling overhead when your process blocks waiting for data.

At low-to-moderate traffic, this overhead is negligible. At high packet rates (hundreds of thousands of packets per second), syscall overhead becomes a significant portion of your CPU budget. Each optimization below removes a layer of this overhead.

-----

## Level 0: Standard UDP Sockets

### How It Works

Your QUIC library calls `sendmsg()` / `recvmsg()` for every packet or small batch. Each call is a full syscall: the CPU transitions from user mode to kernel mode, the kernel copies data between its socket buffers and your userspace buffer, and control returns to your process.

```
Application          Kernel                    NIC
    |                  |                        |
    |-- sendmsg() --> [syscall boundary]        |
    |                  |-- socket buffer ------->|
    |                  |-- netfilter hooks       |
    |                  |-- routing lookup        |
    |                  |-- driver queue -------->|
    |<- return ------  |                        |
```

### Syscall Cost

One `sendmsg()` or `recvmsg()` per packet (or per small batch with `sendmmsg()` / `recvmmsg()`). Each syscall costs roughly 1–5 microseconds of overhead depending on CPU and kernel version.

### Container Feasibility

Works everywhere with zero configuration. Default seccomp profiles allow all standard socket syscalls. No elevated capabilities required. This is your baseline.

### Rust Ecosystem

Standard library `std::net::UdpSocket`, or async equivalents via `tokio::net::UdpSocket` or `mio`. The QUIC libraries (quinn, quiche, s2n-quic) all use this by default.

### When to Move Beyond This

When profiling shows that syscall overhead (visible as time spent in `__x64_sys_sendmsg` / `__x64_sys_recvmsg` in perf traces) exceeds 5–10% of your CPU time under load.

-----

## Level 1: `io_uring`

### How It Works

`io_uring` replaces the per-packet syscall pattern with a shared memory ring buffer pair between your process and the kernel. You post I/O operations (sends, receives) to a **submission queue (SQ)** and the kernel posts results to a **completion queue (CQ)**. Both queues live in memory shared via `mmap` between your process and the kernel.

```
Application                 Kernel                    NIC
    |                         |                        |
    |-- write SQ entry -->  [shared memory]            |
    |-- write SQ entry -->  [shared memory]            |
    |-- write SQ entry -->  [shared memory]            |
    |                         |                        |
    |-- io_uring_enter() --> [single syscall]           |
    |                         |-- process batch ------->|
    |                         |-- process batch ------->|
    |                         |-- process batch ------->|
    |                         |                        |
    |<- read CQ entries --- [shared memory]            |
    |<- read CQ entries --- [shared memory]            |
```

The critical optimization: in polling mode (`IORING_SETUP_SQPOLL`), the kernel runs a dedicated thread that continuously checks the submission queue. Your process can submit work and reap completions **without any syscall at all** — everything happens through shared memory reads and writes.

### Syscall Reduction

Without `SQPOLL`: one `io_uring_enter()` syscall per batch (regardless of batch size) instead of one syscall per packet. If you batch 64 packets per submission, that's a 64x reduction.

With `SQPOLL`: zero syscalls in the steady state. The kernel polling thread picks up submissions automatically. A syscall is only needed if the polling thread goes to sleep after an idle period.

### How It Interacts with the Kernel Network Stack

Packets still traverse the full kernel networking stack (netfilter, routing, socket buffers). The optimization is purely on the syscall/scheduling boundary. The kernel still does the same work per packet internally; you're just amortizing the cost of asking it to do that work.

### Container Feasibility

**Low friction.** `io_uring` is a syscall interface, not a capability or device. The main requirement is adjusting the container's seccomp profile.

Required seccomp allowlist additions:

- `io_uring_setup`
- `io_uring_enter`
- `io_uring_register`

In Docker, this means either running with `--security-opt seccomp=unconfined` (not recommended for production) or providing a custom seccomp profile JSON that adds these three syscalls to the allowlist. In Kubernetes, you'd use a `seccompProfile` in the pod's security context pointing to a custom profile on the node.

No elevated capabilities required. No changes to network namespace configuration. No special device access. Works with standard container networking (veth pairs, bridge networks, overlay networks).

**Caveat:** `io_uring` has been a frequent source of Linux kernel CVEs (privilege escalation, information leaks). Some hardened environments (GKE Autopilot, some managed Kubernetes providers) disable it entirely. Check your environment before depending on it.

### Rust Ecosystem

- `io-uring` crate: low-level bindings to the Linux io_uring interface
- `tokio-uring` crate: async runtime integration with tokio
- `monoio`: an io_uring-first async runtime (alternative to tokio for io_uring-native workloads)

### Integration Pattern

Your QUIC library's I/O layer would be abstracted behind a trait, with a standard socket implementation and an io_uring implementation. At startup, detect whether io_uring is available (the `io_uring_setup` syscall will fail with `ENOSYS` on older kernels or when blocked by seccomp) and select the appropriate backend.

```rust
trait QuicSocket {
    async fn send_packets(&self, packets: &[OutgoingPacket]) -> io::Result<usize>;
    async fn recv_packets(&self, buf: &mut [IncomingPacket]) -> io::Result<usize>;
}

struct StandardSocket { fd: UdpSocket }
struct IoUringSocket { ring: IoUring, fd: RawFd }
```

-----

## Level 2: AF_XDP

### How It Works

AF_XDP (Address Family XDP) is a socket type that provides a path from the NIC driver directly to userspace, bypassing the kernel's entire networking stack. It works in conjunction with an XDP (eXpress Data Path) eBPF program that runs at the driver level.

The architecture has four components:

1. **XDP program (eBPF):** A small program loaded into the NIC driver that inspects each incoming packet at the earliest possible point and decides whether to redirect it to your AF_XDP socket or let it continue through the normal kernel stack.
1. **UMEM:** A shared memory region (allocated by your process, registered with the kernel) where packet data lives. The NIC's DMA engine writes incoming packet data directly into this region. Your process reads from and writes to this same region.
1. **Ring buffers:** Four ring buffers manage the UMEM — FILL (userspace tells kernel which UMEM frames are available for receiving), RX (kernel tells userspace which frames have received data), TX (userspace tells kernel which frames to transmit), COMPLETION (kernel tells userspace which transmitted frames are done).
1. **Your application:** Reads packet data directly from UMEM frames referenced by RX ring entries.

```
NIC Hardware                 Kernel (driver level)         Userspace
    |                            |                            |
    |-- DMA write to UMEM ------>|                            |
    |                            |                            |
    |                        [XDP/eBPF program]               |
    |                            |                            |
    |                            |-- "UDP 443? redirect" ---->|
    |                            |                            |
    |                            |   [RX ring entry posted]   |
    |                            |                            |
    |                            |            [app reads UMEM frame]
    |                            |                            |
    |                            |            [app writes UMEM frame]
    |                            |            [TX ring entry posted]
    |                            |                            |
    |<-- DMA read from UMEM -----|                            |
```

### Syscall Reduction

In the steady state, **zero syscalls** for packet I/O. Your process and the kernel communicate entirely through shared memory ring buffers. The `poll()` or `sendto()` syscall is only needed to wake the kernel if it has gone idle (similar to io_uring's SQPOLL).

The packet data itself is **zero-copy** — the NIC writes it into UMEM via DMA, and your process reads it from the same memory location. No `memcpy` between kernel and userspace buffers.

### What It Bypasses

Unlike `io_uring` (which still traverses the full kernel network stack), AF_XDP bypasses:

- Socket buffer allocation (`sk_buff`)
- The kernel's protocol processing (no UDP socket lookup in kernel)
- Netfilter / iptables hooks
- Connection tracking (`conntrack`)
- The kernel's routing table (for received packets)
- qdisc / traffic control (for transmitted packets)

The XDP eBPF program is the *only* kernel code that touches your packets. Everything else is between the NIC hardware and your application.

### The XDP Filter Program

A minimal XDP program for a QUIC server might look like:

```c
// Pseudocode for the eBPF XDP program
// Compiled with clang and loaded via libbpf

SEC("xdp")
int quic_filter(struct xdp_md *ctx) {
    // Parse ethernet header
    // Parse IP header
    // Parse UDP header
    // If destination port == 443:
    //     return bpf_redirect_map(&xsks_map, queue_index, 0);
    // Else:
    //     return XDP_PASS;  // let kernel handle non-QUIC traffic
}
```

This means your container's normal networking (DNS resolution, health check endpoints, metrics scrapers) still works through the kernel stack. Only UDP port 443 traffic gets the fast path.

### Container Feasibility

**High friction.** AF_XDP requires significantly more access than io_uring.

Required Linux capabilities:

- `CAP_NET_ADMIN` — for creating AF_XDP sockets and attaching XDP programs
- `CAP_BPF` (or `CAP_SYS_ADMIN` on older kernels) — for loading eBPF programs
- `CAP_NET_RAW` — for raw packet access

Required device/resource access:

- Access to the network interface (ideally not a veth)
- Hugepages for UMEM allocation (optional but recommended for performance)
- BPF filesystem mounted (`/sys/fs/bpf`) for pinning maps

#### Container networking considerations:

**Default container networking (veth pair):** AF_XDP works on veth interfaces, but the performance benefit is substantially reduced. Packets still traverse the host-side virtual networking before reaching the veth, and XDP on veth is limited to "generic" mode (SKB-based) rather than native driver mode. You get the zero-copy UMEM benefit but not the full driver-level bypass.

**macvlan:** A macvlan sub-interface gives the container a direct presence on the host's physical network. XDP can run in native mode on some macvlan configurations. Better than veth but still not ideal.

**SR-IOV Virtual Function:** The gold standard for containers. The NIC presents a hardware-isolated virtual function that is passed directly into the container. XDP runs in native driver mode on the VF. The container sees what looks like a dedicated physical NIC. Kubernetes supports this via the SR-IOV device plugin and Multus CNI for attaching the VF as a secondary interface.

**Host networking (`--net=host` / `hostNetwork: true`):** The container shares the host's network namespace and sees the physical NIC directly. XDP runs in full native mode. Simplest to set up but sacrifices network isolation.

#### Kubernetes deployment pattern:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: quic-server
spec:
  hostNetwork: false
  containers:
  - name: quic
    securityContext:
      capabilities:
        add: ["NET_ADMIN", "BPF", "NET_RAW"]
    resources:
      limits:
        # SR-IOV VF allocated by device plugin
        intel.com/sriov_netdevice: "1"
    volumeMounts:
    - name: bpf-fs
      mountPath: /sys/fs/bpf
    - name: hugepages
      mountPath: /dev/hugepages
  volumes:
  - name: bpf-fs
    hostPath:
      path: /sys/fs/bpf
  - name: hugepages
    emptyDir:
      medium: HugePages
```

### Rust Ecosystem

- `af_xdp` crate: Rust bindings for AF_XDP sockets
- `aya` crate: Pure Rust eBPF library (write XDP programs in Rust, no clang/LLVM toolchain needed)
- `libbpf-rs`: Rust bindings to libbpf for loading eBPF programs

The `aya` + `af_xdp` combination is particularly compelling — you can write both your XDP filter and your QUIC server in Rust, sharing type definitions for packet parsing.

-----

## Comparison Matrix

| Dimension                  | Standard Sockets         | io_uring                      | AF_XDP                                    |
|----------------------------|--------------------------|-------------------------------|-------------------------------------------|
| Syscalls per packet        | 1 (or 1 per small batch) | 1 per batch or 0 with SQPOLL | 0 in steady state                         |
| Data copies                | Kernel <-> userspace copy| Kernel <-> userspace copy     | Zero-copy (shared UMEM)                   |
| Kernel stack traversal     | Full                     | Full                          | Bypassed (XDP filter only)                |
| Container capabilities     | None                     | None (seccomp adjustment)     | NET_ADMIN, BPF, NET_RAW                   |
| Works with veth            | Yes                      | Yes                           | Degraded (generic XDP only)               |
| Works with SR-IOV VF       | Yes                      | Yes                           | Full native performance                   |
| Works with host networking | Yes                      | Yes                           | Full native performance                   |
| Kernel version minimum     | Any                      | 5.1+ (5.11+ for SQPOLL fixes)| 4.18+ (5.4+ recommended)                  |
| Rust crate maturity        | Stable (std/tokio)       | Maturing (tokio-uring)        | Early but functional (aya, af_xdp)        |
| Risk profile in production | None                     | Low (seccomp CVE surface)     | Moderate (elevated caps, eBPF complexity) |

-----

## Recommended Progression

**Phase 1 — Baseline.** Build the QUIC library with standard async UDP sockets (tokio). Get the protocol implementation correct. Profile under load and establish baseline syscall overhead.

**Phase 2 — io_uring backend.** Abstract the I/O layer behind a trait. Implement an io_uring backend using `tokio-uring` or the `io-uring` crate directly. Feature-gate it behind a cargo feature flag. Measure syscall reduction with `perf stat` (count `syscalls:sys_enter_sendmsg` vs `syscalls:sys_enter_io_uring_enter`). Document the seccomp profile adjustment for container deployments.

**Phase 3 — AF_XDP backend.** Write the XDP filter in Rust using `aya`. Implement the AF_XDP socket backend using the `af_xdp` crate. This is the experimental/advanced tier — feature-gated, documented with container deployment requirements, and tested primarily with SR-IOV or host networking. Benchmark against io_uring to quantify the benefit of kernel stack bypass.

Each phase is independently useful, each teaches something different about where time is spent, and each can be enabled or disabled based on the deployment environment's constraints.

-----

## Measuring the Difference

Key metrics to track across all three levels:

- **Syscalls per second:** `perf stat -e syscalls:sys_enter_* -p <pid>` — the most direct measure of kernel boundary crossings
- **CPU time in kernel vs userspace:** `perf top` or `perf record` — look for time in `__x64_sys_sendmsg`, `__x64_sys_recvmsg`, `__do_sys_io_uring_enter`, `napi_gro_receive`, `netfilter_*`
- **Packets per second at saturation:** the throughput ceiling before CPU becomes the bottleneck
- **p99 latency per packet:** kernel stack traversal adds jitter; bypassing it reduces tail latency
- **Context switches per second:** `pidstat -w` — io_uring and AF_XDP should dramatically reduce involuntary context switches related to I/O

-----

## Further Reading

- **io_uring:** Jens Axboe's "Efficient I/O with io_uring" (kernel.dk), Lord of the io_uring guide (unixism.net)
- **AF_XDP:** "AF_XDP" in the Linux kernel documentation (kernel.org/doc/html/latest/networking/af_xdp.html)
- **XDP in Rust:** Aya Book (aya-rs.dev/book/)
- **QUIC + kernel bypass:** Cloudflare's blog posts on QUIC performance and XDP integration
- **Container networking with SR-IOV:** Kubernetes SR-IOV device plugin documentation
