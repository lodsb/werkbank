# Korad KA3005P / KA3005PEA — serial protocol reference

Extracted from the [sigrok wiki](https://sigrok.org/wiki/Korad_KAxxxxP_series).
Implemented in `src/werkbank/korad_ka3005.py`.

Applies to the Korad KAxxxxP family and its many rebadges (Tenma 72-xxxx, RS,
Velleman, Stamos). The **KA3005PEA** adds Fernmessung (4-wire remote sense),
0–10 V analog programming and remote on/off on top of the same command set.

## Serial settings

| Parameter | Value |
|---|---|
| Baud rate | 9600 (classic KA3005P) — **but 115200 on the KA3005PEA**, see bench findings below |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |
| Line terminator | **None** — see below |

## Command set

Channel number is `1` on single-output units.

| Command | Type | Response | Meaning |
|---|---|---|---|
| `*IDN?` | query | `KORADKA3005PV2.0` | Identification |
| `STATUS?` | query | one status byte | See bit table below |
| `VSET1:XX.XX` | command | none | Set output voltage |
| `VSET1?` | query | `XX.XX` | Read the voltage *setpoint* |
| `VOUT1?` | query | `XX.XX` | Read *actual* output voltage |
| `ISET1:X.XXX` | command | none | Set current limit |
| `ISET1?` | query | `X.XXX` | Read the current *setpoint* |
| `IOUT1?` | query | `X.XXX` | Read *actual* output current |
| `OUT1` / `OUT0` | command | none | Output on / off |
| `OVP1` / `OVP0` | command | none | Over-voltage protection on / off |
| `OCP1` / `OCP0` | command | none | Over-current protection on / off |

Ranges: voltage 0–30 V, current 0–5 A.

### STATUS? bits

Only these are considered reliable across firmware revisions:

| Bit | Meaning |
|---|---|
| `0x01` | Channel 1 mode — set = **CV**, clear = **CC** |
| `0x20` | OVP / OCP active |
| `0x40` | Output on/off |

## Quirks that the driver has to handle

These are the reason a naive implementation misbehaves:

1. **No line terminator, in either direction.** You cannot use `readline()`.
   Responses are fixed-width and must be collected over a short time window.
2. **`ISET1?` returns a spurious sixth byte** on many units running protocol
   v2.0 (reportedly fixed in v2.1). This is *not* safely handled by "parse the
   leading float" — when the stray byte is a digit, `0.500` + `1` parses as
   `0.5001`. The driver parses replies at their **fixed width** (5 chars)
   instead. On this project that value is integrated into charge, so the error
   would land directly in the coulometry.
3. **Timing sensitivity.** The firmware drops or concatenates replies if
   commands are issued back-to-back; the driver inserts a short inter-command
   delay (~60 ms default).
4. **Fixed-width response formats** — `XX.XX` for volts, `X.XXX` for amps. Both
   are 5 characters, which is what makes the width-based parse above simple.

## Bench findings — where this unit deviates from the reference

Verified 2026-08-03 against the actual PSU (read-only queries, output off):

| Item | sigrok reference | This unit |
|---|---|---|
| Model / firmware | KA3005P, protocol v2.0 | `KORAD KA3005PEA V1.5 SN:00134176` |
| Baud rate | 9600 | **115200** |
| `*IDN?` format | `KORADKA3005PV2.0` (no spaces) | `KORAD KA3005PEA V1.5 SN:00134176` (spaced, with serial) |
| `ISET1?` sixth byte | present on many v2.0 units | **not present** — clean 5-byte `'0.500'` |
| `STATUS?` at rest | — | `0x10` set (bit outside the documented reliable set; believed to be the beeper flag, harmless) |

Two consequences:

- **Don't hardcode the baud rate.** `instruments.py` probes both 115200 and 9600
  and identifies by `*IDN?`, so `autodetect()` is the entry point to use.
- The fixed-width parse is retained even though V1.5 doesn't show the sixth-byte
  bug — it costs nothing and protects against a firmware change or a different unit.

**`STATUS?` `mode` is only meaningful while the output is ON.** With the output
off it reads `CC` spuriously, because the CV/CC bit is simply clear. Check the
`output` bit before believing `mode`.

## Project-specific usage notes

- The Korad is the **actuator only**. Its `VOUT1?`/`IOUT1?` readback is used for
  coulometry and sanity checks, never as the trusted measurement — that is the
  Owon's job. See `software/control/potentiostat.py`.
- **`ISET1:` is a safety interlock**, not just a setting. Always set the current
  limit *before* enabling the output into a cell. **Verified 2026-08-05** in the
  two-supply topology, where it matters most: with the Korad commanded to 0.000 V
  and a second supply pushing ~17 mA through the shared node, a 5 mA ISET pinned
  the current at 5.00 mA and `STATUS?` reported CC throughout. Current limiting
  acts on the series pass element, not on the voltage setpoint, so a setpoint
  sitting at its 0 V floor does **not** mean the clamp is out of authority.
  (Tested envelope: 5 mA against ~17 mA available. Not evidence about amp-scale
  external drive.) See `documentation/lab-notebook/2026-08-04.md` §A3.3c.
- Fernmessung is available on the PEA but is deliberately **not** used to sense
  the reference electrode — see `experiments/phase-0/setup/bath-and-mounting.md`
  §Three-electrode wiring topology for why.
- The output is **single-polarity (0–30 V)**. Thief-mode's steal↔deplate sign
  flip therefore needs a physical polarity reversal (manual banana swap in
  Phase 0; relay/H-bridge at Stage 2).

## Bench findings: KA3005PEA V1.5 (2026-11-12)

- `*IDN?` replies `KORAD KA3005PEA V1.5 SN:...`.
- `STATUS?` replies **0xF1** with the output on, regulating CV, OCP + OVP
  armed and not tripped: 0x01 CV, 0x10 (beeper?), **0x20 set while
  protection is ARMED, not only when tripped**, 0x40 output on, and the
  undocumented **0x80**.
- **Read `STATUS?` as a raw byte.** Through a text decode, 0x80 becomes
  U+FFFD (0xFFFD) on every read, and `strip()` would delete a status byte
  that is whitespace (0x20 alone). The driver did both until this date:
  `status()` now uses `query_bytes()`.
