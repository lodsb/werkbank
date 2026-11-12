"""Thin serial driver for the Owon XDM1041 bench DMM.

The XDM1041 presents as a USB CDC virtual COM port and speaks a SCPI-like
command set terminated with newline. The exact spelling of the "read the
measurement" command varies a little between firmware revisions, so this
driver keeps a small fallback chain and `python -m werkbank.instruments` can
be used to confirm which one your unit answers.

Role (as first used, in the ecam project): the Owon is the *trusted
measurement* side. It reads; it never actuates.
"""

import re
import time

import serial  # pyserial
from serial.tools import list_ports


# Candidate SCPI queries that return the primary DC-volts reading, most
# standard first. discover_instruments.py probes this same list.
VDC_READ_CANDIDATES = (
    "MEAS:VOLT:DC?",
    "MEASure:VOLTage:DC?",
    "MEAS?",
    "READ?",
    "FETC?",
    "VAL1?",
)

_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def parse_float(response: str):
    """Pull the first float out of an instrument response, or None.

    Handles bare numbers, scientific notation (+2.877000E-01), and
    responses that carry trailing units/labels.
    """
    if not response:
        return None
    m = _FLOAT_RE.search(response)
    return float(m.group(0)) if m else None


def find_ports():
    """Return a list of (device, description, hwid) for all serial ports."""
    return [(p.device, p.description, p.hwid) for p in list_ports.comports()]


class OwonXDM1041:
    def __init__(self, port, baudrate=115200, timeout=2.0, read_cmd=None):
        # USB-CDC ignores the baud rate, but pyserial still wants a value.
        self.ser = serial.Serial(port, baudrate, timeout=timeout)
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self._read_cmd = read_cmd  # if known; else resolved on first read

    # --- low level -------------------------------------------------------
    def write(self, cmd, settle=0.05):
        self.ser.write((cmd + "\n").encode("ascii"))
        self.ser.flush()
        if settle:
            time.sleep(settle)

    def query(self, cmd, settle=0.1):
        self.write(cmd, settle=0)
        time.sleep(settle)
        return self.ser.readline().decode("ascii", errors="replace").strip()

    # --- high level ------------------------------------------------------
    def idn(self):
        return self.query("*IDN?")

    def configure_vdc(self, vrange=None):
        """Put the meter into DC-volts mode. `vrange` optional (e.g. '2')."""
        self.write(f"CONF:VOLT:DC {vrange}".strip())
        time.sleep(0.3)

    def _resolve_read_cmd(self):
        """Find a read command this firmware actually answers with a number."""
        for cmd in VDC_READ_CANDIDATES:
            if parse_float(self.query(cmd)) is not None:
                self._read_cmd = cmd
                return cmd
        raise RuntimeError(
            "No known DC-volts read command returned a number. "
            "Run discover_instruments.py to inspect the raw responses."
        )

    def resolve_read_cmd(self):
        """Determine (and cache) which read command this firmware answers.

        Called lazily by read_vdc(); call it explicitly if you want to know
        the resolved command up front (e.g. to log it before a run starts).
        """
        return self._read_cmd or self._resolve_read_cmd()

    def configure_resistance(self, vrange=None):
        """Put the meter into 2-wire resistance mode.

        Lead resistance is included, which is ~0.2 Ω on typical probes. On a
        100 Ω reference resistor that is 0.2 % and can simply be subtracted
        after shorting the probes; on anything of ohm order it would dominate,
        so do not use this mode to measure a shunt.
        """
        self.write(f"CONF:RES {vrange}".strip())
        time.sleep(0.5)     # range hunting is slower than on volts

    def read_value(self):
        """Read in whatever mode the meter is currently in.

        The resolved read command (`MEAS?` and friends) returns the present
        measurement regardless of function, so the same command serves volts
        and ohms. Only the interpretation differs, which is the caller's job.
        """
        if self._read_cmd is None:
            self._resolve_read_cmd()
        val = parse_float(self.query(self._read_cmd))
        if val is None:
            # firmware hiccup — re-resolve once
            self._resolve_read_cmd()
            val = parse_float(self.query(self._read_cmd))
        return val

    def read_vdc(self):
        """Return the present DC voltage as a float (volts)."""
        return self.read_value()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @classmethod
    def autodetect(cls, exclude=(), timeout=2.0):
        """Find the Owon by *IDN? (not by COM number) and open it.

        Returns (instrument, Detected). Raises if not found. `exclude` is a
        list of ports to leave untouched (e.g. a parallel project's port).
        """
        from instruments import find_owon
        d = find_owon(exclude=exclude)
        if d is None:
            raise RuntimeError(
                "Owon XDM1041 not found on any serial port. "
                "Is it connected? Run instruments.py to see what's on the bus."
            )
        return cls(d.port, baudrate=d.baud, timeout=timeout), d
