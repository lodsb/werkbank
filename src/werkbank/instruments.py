"""Identify bench instruments by *identity*, not by COM number.

COM numbers shuffle on replug and a parallel hardware project shares the USB
bus, so addressing an instrument by "COM3" is fragile and occasionally
dangerous (you could drive the wrong device). Instead we ask each port
`*IDN?` and match the reply against a known signature. Ports that don't match
-- including the neighbouring project -- are left alone.

Design notes / real-world wrinkles handled here:
- Mixed dialects: the Owon is newline-terminated SCPI (115200); the Korad
  KA3005 is non-newline-terminated and often 9600. The prober tries both
  bauds and both terminators and matches on whatever answers.
- Neighbour safety: ports are opened with DTR/RTS deasserted so we don't
  reset an Arduino-class board on the shared bus; if a port is already held
  open by another app it simply won't open and we skip it. Pass `exclude=`
  (or --exclude on the CLI) to never touch known-neighbour ports at all.

CLI:
    python instruments.py                    # identify everything it can
    python instruments.py --exclude COM9     # leave the parallel project alone
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from typing import Optional, Sequence

import serial
from serial.tools import list_ports


@dataclass(frozen=True)
class InstrumentSignature:
    name: str
    idn_regex: str  # matched (case-insensitive) against the *IDN? reply


# Known bench instruments. Regexes are permissive on purpose -- firmware
# strings vary in punctuation/spacing across revisions.
OWON_XDM1041 = InstrumentSignature("Owon XDM1041", r"OWON.*XDM\s*1041")
KORAD_KA3005 = InstrumentSignature("Korad KA3005", r"KORAD.*KA\s*3005|KA3005P")
KNOWN_SIGNATURES = (OWON_XDM1041, KORAD_KA3005)

PROBE_BAUDS = (115200, 9600)
PROBE_TERMINATORS = ("\n", "")  # Owon needs \n; Korad wants none


@dataclass
class Detected:
    signature: InstrumentSignature
    port: str
    baud: int
    idn: str

    @property
    def name(self) -> str:
        return self.signature.name


def _open_quiet(port: str, baud: int, timeout: float) -> serial.Serial:
    """Open a port without toggling control lines (avoids Arduino auto-reset)."""
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = timeout
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def _read_for(ser: serial.Serial, seconds: float = 0.4) -> str:
    """Accumulate whatever bytes arrive within a window (works for both
    newline- and non-newline-terminated instruments)."""
    end = time.monotonic() + seconds
    buf = bytearray()
    while time.monotonic() < end:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n))
        else:
            time.sleep(0.02)
    return buf.decode("ascii", errors="replace")


def _match(resp: str) -> Optional[InstrumentSignature]:
    for sig in KNOWN_SIGNATURES:
        if re.search(sig.idn_regex, resp, re.IGNORECASE):
            return sig
    return None


def probe_port(port: str) -> Optional[Detected]:
    """Ask a single port who it is. Returns Detected or None.

    None means: couldn't open (in use / no permission), or nothing we
    recognise answered. Never raises for the common failure paths.
    """
    for baud in PROBE_BAUDS:
        try:
            ser = _open_quiet(port, baud, timeout=0.3)
        except (serial.SerialException, OSError):
            return None  # busy or inaccessible -> leave it alone
        try:
            for term in PROBE_TERMINATORS:
                ser.reset_input_buffer()
                ser.write(("*IDN?" + term).encode("ascii"))
                ser.flush()
                time.sleep(0.2)
                resp = _read_for(ser, 0.4)
                sig = _match(resp)
                if sig:
                    return Detected(sig, port, baud, resp.strip())
        finally:
            ser.close()
    return None


def identify_all(exclude: Sequence[str] = ()):
    """Probe every serial port. Returns a list of dicts describing each:
    {port, description, detected(Detected|None), excluded(bool)}."""
    exclude = {p.upper() for p in exclude}
    results = []
    for p in list_ports.comports():
        excluded = p.device.upper() in exclude
        detected = None if excluded else probe_port(p.device)
        results.append(
            {"port": p.device, "description": p.description,
             "detected": detected, "excluded": excluded}
        )
    return results


def find(signature: InstrumentSignature, exclude: Sequence[str] = ()) -> Optional[Detected]:
    """Return the first port whose *IDN? matches `signature` (or None)."""
    exclude = {p.upper() for p in exclude}
    for p in list_ports.comports():
        if p.device.upper() in exclude:
            continue
        d = probe_port(p.device)
        if d and d.signature is signature:
            return d
    return None


def find_owon(exclude: Sequence[str] = ()) -> Optional[Detected]:
    return find(OWON_XDM1041, exclude=exclude)


def find_korad(exclude: Sequence[str] = ()) -> Optional[Detected]:
    return find(KORAD_KA3005, exclude=exclude)


def _cli():
    ap = argparse.ArgumentParser(description="Identify bench serial instruments by *IDN?.")
    ap.add_argument("--exclude", default="", help="Comma-separated ports to leave untouched, e.g. COM9.")
    args = ap.parse_args()
    exclude = [s.strip() for s in args.exclude.split(",") if s.strip()]

    rows = identify_all(exclude=exclude)
    if not rows:
        print("No serial ports found.")
        return
    print(f"{'PORT':<8} {'IDENTIFIED AS':<20} {'BAUD':<7} DETAIL")
    print("-" * 78)
    for r in rows:
        if r["excluded"]:
            ident, baud, detail = "(excluded)", "", r["description"]
        elif r["detected"]:
            d = r["detected"]
            ident, baud, detail = d.name, str(d.baud), d.idn
        else:
            ident, baud, detail = "unknown", "", r["description"]
        print(f"{r['port']:<8} {ident:<20} {baud:<7} {detail}")
    print("\nInstruments are matched by their *IDN? reply, so COM numbers can move freely.")


if __name__ == "__main__":
    _cli()
