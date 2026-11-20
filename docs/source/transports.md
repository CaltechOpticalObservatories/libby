# Choosing a transport

Libby runs the same keywords, `Client`, CLI and daemons over RabbitMQ or
Cyclone DDS; switching is one `transport` setting (see {doc}`daemon`). This
page compares the two. The ZMQ transport needs an address book and is not
covered here.

## At a glance

| | RabbitMQ | Cyclone DDS |
|---|---|---|
| topology | brokered: every message goes through the broker | peer to peer: messages go straight between peers |
| infrastructure | a RabbitMQ server | none |
| finding peers | the broker routes by queue name | DDS discovery (multicast, or unicast peers listed in Cyclone XML) |
| single point of failure | the broker | none |
| install | `pika` (core dependency) | the `cyclonedds` extra; on Python 3.14, a source build (see {doc}`installation`) |
| monitoring | broker management UI | DDS tools can read the typed envelope |

## Measured performance

Both transports delivered every request and every publish in every run. One
host (i7-14700K, 28 threads), Python 3.14, 200-byte payloads, end to end
through `Client` and `LibbyDaemon`:

| scenario | RabbitMQ | Cyclone DDS |
|---|---|---|
| `show`, back to back | 2.7k/s, p50 318 µs | 6.4–6.7k/s, p50 126–133 µs |
| `modify`, back to back | 3.0k/s, p50 305 µs | 7.0–7.2k/s, p50 122 µs |
| publish to 1 subscriber | 30.6k/s | 52–57k/s |
| publish to 10 subscribers | 2.9k/s | 2.6–5.3k/s |
| 512 KB binary messages | 0.58 GB/s | 0.99–1.08 GB/s |

80 daemons on the same host, each showing keywords on random peers at a
fixed rate:

| | RabbitMQ | Cyclone DDS |
|---|---|---|
| on schedule, no timeouts, up to | 8k requests/s total | 24k requests/s total |
| saturates at | 14–15k/s (the broker) | 48–51k/s (host CPU) |
| all peers reachable after launch | 1.8 s | 3.2 s |

Past saturation both fall behind schedule rather than failing requests.

Publishing to several subscribers costs about the same on both: `publish`
sends one message per subscriber, so the rate drops with each one added.

## Which to choose

- **Cyclone DDS** for many peers, high request rates or large binary
  payloads, and wherever running a broker is unwanted.
- **RabbitMQ** when a broker already runs or its management UI is wanted,
  when the network blocks multicast and listing peers in Cyclone XML is
  unwelcome, or when load stays well under the broker's limit.

A Cyclone DDS peer's `start()` waits up to 1 s (about 100 ms on a quiet
network) for discovery to settle, so a first broadcast reaches peers that are
already up.
