# werkbank

Serial drivers and port discovery for bench instruments.

| module | instrument | dialect |
|---|---|---|
| `werkbank.korad_ka3005` | Korad KA3005P / KA3005PEA power supply (and rebadges) | no terminator; 9600 classic, **115200 on the PEA** |
| `werkbank.owon_xdm1041` | OWON XDM1041 bench multimeter | newline-terminated SCPI, 115200 |
| `werkbank.instruments` | identify every serial port by its `*IDN?` reply | tries both terminators |

```
pip install -e path/to/werkbank
python -m werkbank.instruments            # what is on which port
python -m werkbank.korad_grid_probe       # the PSU's real setpoint resolution (output never enabled)
```

## What lives here, and what does not

**Here:** talking to an instrument — commands, parsing, quirks, discovery.

**Not here: safety limits.** The ceiling that matters is set by whatever is
wired to the terminals, which only the project using the supply knows. A
project wraps these drivers with its own limits (and should make them
*refuse* an out-of-range request rather than clamp it). Commissioning
procedures that energise the output belong to the project too.

## Quirks worth knowing before you trust a reading

- **Port discovery opens every port it is not told to skip**, and one
  unresponsive port hangs the whole scan. Always pass `exclude=` for ports
  you know (e.g. your microcontroller's CDC), or give the port explicitly.
- **Korad setpoints are on a 10 mV grid**; the firmware truncates rather
  than rounds (`korad_grid_probe` measures it).
- **Korad `STATUS?` is one raw byte — read it as bytes.** A KA3005PEA V1.5
  sets an undocumented 0x80 bit; through a text decode that became U+FFFD
  on every read. Fixed in `status()` (`query_bytes`), pinned by
  `tests/test_korad_status.py`. On that unit 0x20 means OVP/OCP *armed*.
- `STATUS?` `mode` (CV/CC) is only meaningful while the output is on.

Full protocol notes: `docs/korad-ka3005-serial-protocol.md`.
