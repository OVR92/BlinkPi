# Blink local storage vs. the internet: what the traffic actually shows

A hands-on look at whether a Blink camera with local storage (Sync Module 2 +
USB drive) keeps working when the internet is down. Short answer: **it does
not.** Local storage saves you the cloud subscription, but the camera still
requires a live, authenticated connection to Blink's servers before it will
record anything, even to the local drive.

All findings below were observed directly from the camera's own network
traffic, by putting the camera on a small router that could capture its
packets and selectively block or redirect them. Nothing was extracted from
the device itself.

## TL;DR

- Recorded clips are stored **locally**: the footage goes camera → Sync
  Module over the LAN and does **not** round-trip to Blink's cloud.
- But **recording is gated on a live cloud session.** With no internet, the
  camera won't start a clip at all, even though the Sync Module is reachable.
- The camera tolerates a **brief** WAN blip (roughly 30–60 seconds) and then
  gives up and reconnects. Sustained outages disable it.
- A local server **cannot** stand in for Blink's cloud. The camera pins the
  server's certificate to Blink's own key, so no self-signed, look-alike, or
  publicly-trusted (e.g. Let's Encrypt) certificate is accepted.

## What was tested

### 1. Where does the clip data go? (normal operation, online)

Triggering a motion clip produces:

- A **local** transfer, camera → Sync Module, TLS on port 443, ~1.5 MB for a
  short clip. This is the footage.
- A few tiny (~69-byte) heartbeat messages to Blink's cloud on a persistent
  TLS connection.

So the footage stays on the LAN. The cloud only sees small control traffic.

### 2. Does it record with the internet down? (camera cut off, LAN intact)

With the camera's internet blocked but the LAN (and the Sync Module) still
reachable:

- **No clip is recorded.** The camera does not even begin recording (no blue
  LED).
- It makes zero connections to the Sync Module during the outage.
- It loops: re-resolve Blink's hostname, retry the cloud once per second, and
  periodically reset its Wi-Fi and rejoin.

Recording is therefore gated on a live, authenticated cloud session, not on
the Sync Module being present.

### 3. How long a blip does it tolerate?

Simulating a full WAN outage of varying length:

| Outage length | Result |
|---|---|
| 30 s | Survived — cloud session held, no reconnect, records normally after |
| 75 s | Tore down — camera gave up and started reconnecting |

The give-up point sits between the two, near the camera's ~30 s cloud
heartbeat interval, so call it **~30–60 seconds**. Genuine short flaps are
ridden out on their own; longer outages are not.

### 4. Can a local server stand in for the cloud? (certificate check)

The camera was pointed at a local server in place of Blink's cloud host, to
see what it requires of the server's certificate. Three certificates were
tried:

1. **Self-signed cert** (arbitrary): rejected with a fatal TLS `unknown_ca`
   alert. The camera validates the server certificate.
2. **Publicly-trusted cert (Let's Encrypt):** would also be rejected. The
   real Blink cloud certificate, seen in the capture, is **self-signed** by
   Blink (issuer = subject, `CN=*.immedia-semi.com, O=Amazon, OU=Blink`, no
   intermediate, no public root). The camera does not use a public-CA trust
   model, so a public certificate is not trusted.
3. **Self-signed look-alike** carrying Blink's exact identity fields
   (`CN=*.immedia-semi.com, O=Amazon, OU=Blink`): still rejected with
   `unknown_ca`.

**Conclusion: the camera pins the server certificate to Blink's actual key,
not to the name on it.** (The `unknown_ca` alert refers to trust anchors in general, not the public CA system: a self-signed certificate is its own trust anchor, and the camera holds only the one Blink certificate, so any other certificate, public or look-alike, fails to match it.) TLS also requires the server to prove it holds the
private key matching the certificate, and that key is Blink's alone. Copying
the certificate doesn't help, and a look-alike with a different key is
refused. A local server cannot present itself as the cloud without Blink's
private key.

## Practical takeaways

- Treat Blink local storage as **subscription-free clip storage, not offline
  operation.** Cutting the internet disables the camera as a recorder.
- The camera survives WAN blips of ~30–60 s by itself. For longer/real-world
  flaps, a **redundant uplink** (so the outage never reaches the camera) is the
  robust fix.
- For a camera that keeps recording with the internet unplugged, use a
  **local-first** system (e.g. RTSP/ONVIF cameras with a local NVR). Recording
  there does not depend on any vendor server.

## Caveats

- Observed on a Blink Mini + Sync Module 2 at the firmware present in
  September 2026. Behavior may differ by model and firmware.
- The camera negotiated only TLS 1.2 with a single cipher
  (`TLS_RSA_WITH_AES_128_CBC_SHA256`), which is worth knowing if you try to
  reproduce the certificate tests.
- This documents the network behavior only. No device firmware was modified or
  extracted.
