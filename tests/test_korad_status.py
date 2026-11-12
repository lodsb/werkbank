"""STATUS? must be decoded from the raw byte. No instrument needed.

Pins the 2026-11-12 bug: through a text decode, 0xF1 (a KA3005PEA V1.5 with
output on, CV, protection armed) read as U+FFFD on every call, and a
whitespace status byte (0x20 alone) would have been stripped to nothing.

    python -m pytest tests      or      python tests/test_korad_status.py
"""
from werkbank.korad_ka3005 import KoradKA3005


class FakeSerial:
    def __init__(self, reply):
        self.reply = reply
        self.pending = b""

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, data):
        self.pending = self.reply

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self.pending)

    def read(self, n):
        out, self.pending = self.pending[:n], self.pending[n:]
        return out


def korad(reply):
    k = object.__new__(KoradKA3005)      # no port is opened
    k.ser = FakeSerial(reply)
    k.settle = 0.0
    return k


def test_high_bit_status_is_read_not_replaced():
    st = korad(b"\xf1").status()
    assert st["raw"] == 0xF1
    assert st["output"] is True and st["mode"] == "CV" and st["ovp_ocp"] is True


def test_whitespace_status_byte_survives():
    st = korad(b"\x20").status()              # OVP/OCP bit alone
    assert st["raw"] == 0x20
    assert st["output"] is False and st["mode"] == "CC" and st["ovp_ocp"] is True


def test_text_replies_unchanged():
    assert korad(b"24.00").query("VSET1?") == "24.00"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
