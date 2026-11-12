"""Thin serial driver for the Korad KA3005P / KA3005PEA bench PSU.

The driver talks to the instrument and nothing more. Safety limits depend on
what is wired to the terminals, so they belong to the caller. Its own VOUT/IOUT
readback is fine for sanity checks; use a separate meter where a reading has to
be trusted.

Protocol notes (Korad KAxxxxP series, per sigrok, plus bench findings):
- 8N1. **Baud differs by model:** sigrok documents 9600 for the classic KA3005P,
  but the **KA3005PEA on this bench runs at 115200** (verified: `KORAD KA3005PEA
  V1.5`). `autodetect()` probes both, which is why it is the
  preferred entry point over constructing with an explicit port.
- **No line terminator on commands or responses**, and no checksum. So we can't
  use readline(); responses are read over a short time window instead.
- Responses are fixed-width: VSET1?/VOUT1? -> 'XX.XX', ISET1?/IOUT1? -> 'X.XXX',
  STATUS? -> a single status byte.
- Firmware quirk: on protocol v2.0 many units return a spurious **sixth byte**
  from `ISET1?`. Handled by parsing replies at their fixed width -- see
  `_parse_fixed`. Grabbing the leading float is not sufficient, because a
  numeric stray byte silently corrupts the value (and anything integrated from it).
- The firmware is timing-sensitive; commands need a short inter-command delay
  or replies get dropped or concatenated.

Deliberately dependency-free (pyserial only) rather than pulling in
py-korad-serial / ka3005p: the protocol is a dozen commands, and matching
owon_xdm1041.py's shape keeps the two legs of the loop symmetric.
"""

from __future__ import annotations

import re
import time

import serial

# Hardware envelope of the KA3005 family. Clamped in software as a first line
# of defence; the PSU's own current limit is the real backstop.
V_MAX = 30.0
I_MAX = 5.0

# Setpoint grids imposed by the protocol's fixed-width fields: VSET1 is 'XX.XX'
# and ISET1 is 'X.XXX'. These are NOT cosmetic -- a caller that asks for 0.166 V
# gets 0.17 V, and if it logs its own request instead of the returned value it
# records a setpoint the instrument never held. See `set_voltage`.
#
# **Verified against the instrument** 2026-08-06 with `korad_grid_probe.py`, on
# KORAD KA3005PEA V1.5. Both grids confirmed at the firmware, not
# merely assumed from the format string: 8/8 informative voltage probes and 3/3
# current probes were quantised.
#
# The firmware **TRUNCATES toward zero**; it does not round. Measured:
# 0.128 -> 0.12, 0.117 -> 0.11, 1.239 -> 1.23. (Ties like 0.125 cannot show this
# -- truncation and round-half-to-even both give 0.12 -- which is why the probe
# carries x.xx7/x.xx8 values.) The setters below are unaffected because they
# format to the grid in Python first, so the instrument never sees a digit to
# discard. Any *raw* `cmd("VSET1:...")` with extra decimals loses up to a full
# LSB. Format first.
V_SETPOINT_LSB = 0.01    # 10 mV
I_SETPOINT_LSB = 0.001   # 1 mA

_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+")

# Bits in the STATUS? byte that the sigrok docs consider reliable.
_ST_CH1_CV = 0x01   # set = constant voltage, clear = constant current
_ST_OVP_OCP = 0x20
_ST_OUTPUT = 0x40


def _parse_float(resp):
    """Leading float out of a response, or None."""
    if not resp:
        return None
    m = _FLOAT_RE.search(resp)
    return float(m.group(0)) if m else None


# Every numeric reply in this protocol is fixed-width: 'XX.XX' (volts) and
# 'X.XXX' (amps) are both 5 characters.
_FIXED_WIDTH = 5


def _parse_fixed(resp, width=_FIXED_WIDTH):
    """Parse a fixed-width numeric reply, ignoring anything past `width`.

    This is what defuses the v2.0 `ISET1?` sixth-byte firmware bug. Parsing the
    leading float alone is NOT enough: when the spurious byte happens to be a
    digit, '0.500' + '1' reads back as 0.5001 instead of 0.500 -- an error that
    looks like a real reading, and lands in anything integrated from it.
    """
    if not resp:
        return None
    return _parse_float(resp[:width])


class KoradKA3005:
    # 115200 matches the KA3005PEA on this bench; classic KA3005P units are
    # 9600. Prefer autodetect(), which probes both and passes the right one.
    def __init__(self, port, baudrate=115200, timeout=0.3, settle=0.06):
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baudrate
        self.ser.timeout = timeout
        # Don't toggle control lines -- avoids resetting anything else sharing
        # the bus, and the Korad doesn't use them.
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.settle = settle
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    # --- low level -------------------------------------------------------
    def cmd(self, s):
        """Send a command that returns nothing."""
        self.ser.write(s.encode("ascii"))  # no terminator, by protocol
        self.ser.flush()
        time.sleep(self.settle)

    def query_bytes(self, s, window=0.25):
        """Send a query and return the RAW reply bytes collected within `window`."""
        self.ser.reset_input_buffer()
        self.ser.write(s.encode("ascii"))
        self.ser.flush()
        end = time.monotonic() + window
        buf = bytearray()
        while time.monotonic() < end:
            n = self.ser.in_waiting
            if n:
                buf.extend(self.ser.read(n))
            else:
                time.sleep(0.01)
        time.sleep(self.settle)
        return bytes(buf)

    def query(self, s, window=0.25):
        """Send a query and return the reply as stripped ASCII text.

        For TEXT replies only (`*IDN?`, `VSET1?`, ...). Never use it for a
        reply that is a raw byte: decoding turns any byte >= 0x80 into
        U+FFFD and `strip()` deletes a byte that happens to be whitespace
        (0x20 is the OVP/OCP bit on its own). Use `query_bytes`.
        """
        return self.query_bytes(s, window).decode("ascii", errors="replace").strip()

    # --- identity / status ----------------------------------------------
    def idn(self):
        return self.query("*IDN?")

    def status(self):
        """Decode STATUS? into the bits that are actually trustworthy.

        Caveat: **`mode` is only meaningful while the output is ON.** With the
        output off this reads 'CC' spuriously (the CV/CC bit is simply clear),
        which looks alarming but isn't. Check `output` first.

        Bits outside the reliable set are ignored; on the PEA, 0x10 is observed
        set at rest (believed to be the beeper flag) and is harmless.

        STATUS? replies with ONE RAW BYTE, so this reads bytes, not text. Until
        2026-11-12 it went through the text `query()`, and a KA3005PEA V1.5
        that sets the undocumented 0x80 bit (it replies 0xF1 with output on,
        CV, protection armed) came back as U+FFFD = 0xFFFD on every read --
        a constant that happens to decode as "output on, CV, OVP/OCP", so it
        looked plausible and would have kept saying CV during a current limit.
        On that unit 0x20 is set while OCP/OVP are ARMED and not tripped.
        """
        raw = self.query_bytes("STATUS?")
        if not raw:
            return {"raw": None, "output": None, "mode": None, "ovp_ocp": None}
        b = raw[0]
        return {
            "raw": b,
            "output": bool(b & _ST_OUTPUT),
            "mode": "CV" if b & _ST_CH1_CV else "CC",
            "ovp_ocp": bool(b & _ST_OVP_OCP),
        }

    # --- setpoints -------------------------------------------------------
    def set_voltage(self, volts):
        """Set the output voltage. **Returns the value actually commanded**,
        which is quantised to the protocol's 10 mV grid (`V_SETPOINT_LSB`).

        Always log the return value, never the request. The formatting is done
        first and parsed back so the returned number is exactly the one that
        went down the wire -- rounding the float separately can disagree with
        `%05.2f` on ties.
        """
        v = max(0.0, min(float(volts), V_MAX))
        field = f"{v:05.2f}"
        self.cmd(f"VSET1:{field}")
        return float(field)

    def set_current(self, amps):
        """Set the current limit. This is the hardware safety clamp -- always
        set it *before* enabling the output into a load.

        Returns the value actually commanded, on the 1 mA grid.
        """
        a = max(0.0, min(float(amps), I_MAX))
        field = f"{a:05.3f}"
        self.cmd(f"ISET1:{field}")
        return float(field)

    def get_voltage_setpoint(self):
        return _parse_fixed(self.query("VSET1?"))

    def get_current_setpoint(self):
        return _parse_fixed(self.query("ISET1?"))

    # --- measurements (actuator-side readback, not the trusted leg) ------
    def measure_voltage(self):
        return _parse_fixed(self.query("VOUT1?"))

    def measure_current(self):
        return _parse_fixed(self.query("IOUT1?"))

    # --- output ----------------------------------------------------------
    def output(self, on):
        self.cmd("OUT1" if on else "OUT0")

    def ocp(self, on):
        self.cmd("OCP1" if on else "OCP0")

    def ovp(self, on):
        self.cmd("OVP1" if on else "OVP0")

    # --- lifecycle -------------------------------------------------------
    def safe_off(self):
        """Best-effort shutdown: output off, setpoint to zero. Never raises."""
        try:
            self.output(False)
            self.set_voltage(0.0)
        except Exception:
            pass

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # Leaving an energised output behind is the one unacceptable failure.
        self.safe_off()
        self.close()

    @classmethod
    def autodetect(cls, exclude=()):
        """Find the Korad by *IDN? (not by COM number) and open it."""
        from instruments import find_korad
        d = find_korad(exclude=exclude)
        if d is None:
            raise RuntimeError(
                "Korad not found on any serial port. Is it powered on and the "
                "USB cable connected (a charge-only cable won't enumerate)? "
                "Run instruments.py to see what's on the bus."
            )
        return cls(d.port, baudrate=d.baud), d
