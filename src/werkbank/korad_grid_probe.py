"""Probe the Korad's real setpoint resolution. Safe: the output is never enabled.

## The question

`korad_ka3005.set_voltage` formats `VSET1` as `XX.XX`, so this code quantises
every setpoint to 10 mV. The whole dither design in `potentiostat.py` exists to
work around that. But the format string is a fact about *this code* -- the
KA3005PEA's firmware may well accept a third decimal, in which case the grid is
1 mV, the dither is unnecessary, and the loop gets ten times finer for free.

Nobody has asked the instrument. This asks it.

## What it does

Writes candidate setpoints with extra decimals and reads back the **raw** reply
string, byte for byte. The readback is the answer: if `VSET1:00.125` comes back
as `00.12` or `00.13`, the firmware quantised it. If it comes back `0.125`, it
did not.

Raw replies matter here. `_parse_fixed` deliberately truncates to five
characters to defuse the `ISET1?` firmware bug, so parsing would hide exactly
the extra digit being looked for.

## Safety

- The output is switched **off** at the start and never enabled.
- The current limit is set to a low value before any voltage is written.
- The original voltage and current setpoints are restored at the end.
- Nothing should be connected to the output terminals, but nothing is driven
  into them even if it is.

Usage:
    python -m werkbank.korad_grid_probe
    python -m werkbank.korad_grid_probe --exclude COM9
"""

from __future__ import annotations

import argparse

from werkbank.korad_ka3005 import KoradKA3005, V_SETPOINT_LSB, I_SETPOINT_LSB

# Values that sit OFF the assumed grid, so the reply distinguishes a finer grid
# from the assumed one. Ties (x.xx5) are included deliberately: they reveal
# whether the firmware rounds or truncates, which are different failure modes.
#
# A probe already ON the assumed grid proves nothing -- it round-trips whatever
# the real grid is. The first version of this script included 00.010 and duly
# reported it as evidence of a finer grid, which it is not. Values are now
# checked for informativeness rather than trusted.
# Note the x.xx8 / x.xx7 entries. Ties like 0.125 CANNOT separate truncation
# from round-half-to-even -- both give 0.12 -- so a probe list of ties alone
# looks conclusive while proving nothing about the rounding rule. Values whose
# truncation and nearest-neighbour differ are the ones that decide it.
V_PROBES = ["00.125", "01.234", "00.005", "12.345", "00.001",
            "00.128", "00.117", "01.239", "00.010"]
I_PROBES = ["0.1234", "0.0005", "0.1238", "0.0010"]


def _on_grid(x, lsb):
    return abs(x / lsb - round(x / lsb)) < 1e-6


def probe(psu, kind, values, lsb):
    """Returns (kept, informative, quantisation_style)."""
    print(f"\n  {kind}1 -- writing extra decimals, reading back raw "
          f"(assumed grid {lsb:g})")
    print(f"  {'written':>10}   {'raw reply':<12} verdict")
    print(f"  {'-'*10}   {'-'*12} {'-'*46}")
    kept = informative = 0
    styles = set()
    for v in values:
        written = float(v)
        psu.cmd(f"{kind}1:{v}")
        raw = psu.query(f"{kind}1?")
        shown = repr(raw) if raw else "<no reply>"

        if _on_grid(written, lsb):
            print(f"  {v:>10}   {shown:<12} (already on the assumed grid -- not informative)")
            continue
        informative += 1

        try:
            got = float(raw) if raw else None
        except ValueError:
            got = None
        if got is None:
            print(f"  {v:>10}   {shown:<12} unparseable reply")
            continue

        if abs(got - written) < 1e-9:
            kept += 1
            print(f"  {v:>10}   {shown:<12} KEPT the extra digit -- finer grid than assumed")
            continue

        # Rounding rule. A value whose truncation and nearest-neighbour agree
        # says nothing about which rule is in use -- report that honestly rather
        # than crediting whichever branch happens to be tested first.
        steps = written / lsb
        trunc = int(steps) * lsb
        near = round(steps) * lsb
        decides = abs(trunc - near) > lsb / 100
        if not decides:
            note = "quantised (rule undetermined -- truncation and rounding agree here)"
        elif abs(got - trunc) < lsb / 100:
            styles.add("truncate")
            note = "quantised (TRUNCATED toward zero)"
        elif abs(got - near) < lsb / 100:
            styles.add("round")
            note = "quantised (ROUNDED to nearest)"
        else:
            styles.add("other")
            note = f"quantised to something unexpected ({got:g})"
        print(f"  {v:>10}   {shown:<12} {note}")
    return kept, informative, styles


def main():
    ap = argparse.ArgumentParser(description="Ask the Korad what its real setpoint grid is.")
    ap.add_argument("--exclude", default="", help="Ports to leave untouched, e.g. COM9.")
    args = ap.parse_args()
    exclude = [s.strip() for s in args.exclude.split(",") if s.strip()]

    psu, det = KoradKA3005.autodetect(exclude=exclude)
    print(f"PSU: {det.port} {det.idn!r}")

    print("\nSafing: output off, current limit 10 mA, saving setpoints ...")
    psu.output(False)
    v0 = psu.query("VSET1?")
    i0 = psu.query("ISET1?")
    print(f"  saved VSET1={v0!r}  ISET1={i0!r}")
    psu.cmd("ISET1:0.010")

    try:
        v_kept, v_n, v_styles = probe(psu, "VSET", V_PROBES, V_SETPOINT_LSB)
        i_kept, i_n, i_styles = probe(psu, "ISET", I_PROBES, I_SETPOINT_LSB)
    finally:
        # Restore whatever was there, and leave the output off regardless.
        if v0:
            psu.cmd(f"VSET1:{float(v0):05.2f}")
        if i0:
            psu.cmd(f"ISET1:{float(i0):05.3f}")
        psu.output(False)
        psu.close()
        print("\nRestored original setpoints; output off.")

    print("\n" + "=" * 68)
    if v_kept == 0 and v_n:
        print(f"  VERDICT: voltage grid is {V_SETPOINT_LSB*1000:g} mV, as assumed "
              f"({v_n}/{v_n} informative probes quantised).")
        print("  No change needed. The dither in potentiostat.py is necessary work.")
    elif v_kept == v_n and v_n:
        print("  VERDICT: the firmware ACCEPTS finer voltage setpoints.")
        print("  Change korad_ka3005.set_voltage to '%06.3f' and V_SETPOINT_LSB to")
        print("  0.001, then re-run test_potentiostat.py -- the dither tests should")
        print("  still pass (dither becomes harmless, not wrong) and the effective")
        print("  resolution improves ~10x. Re-check the deadband warning threshold.")
    else:
        print(f"  VERDICT: MIXED -- {v_kept}/{v_n} informative probes kept the digit.")
        print("  Do not act without reading the raw replies above. Partial acceptance")
        print("  usually means the reply field is fixed-width while the write is not,")
        print("  which is worse than either clean answer: the instrument would hold a")
        print("  setpoint it cannot report, so readback silently disagrees with")
        print("  reality. If so, keep the coarser grid.")
    print(f"  current grid: {i_kept}/{i_n} informative probes kept the extra digit")

    styles = v_styles | i_styles
    print()
    if styles == {"truncate"}:
        print("  ROUNDING RULE: the firmware TRUNCATES toward zero.")
        print("  Matters only for a raw cmd('VSET1:...') carrying extra decimals --")
        print("  0.129 becomes 0.12, not 0.13, so a naive write loses up to a full")
        print("  LSB. set_voltage/set_current are unaffected: they format to the grid")
        print("  before sending, so the instrument never sees a digit to discard.")
    elif styles == {"round"}:
        print("  ROUNDING RULE: the firmware ROUNDS to nearest -- the forgiving case,")
        print("  and it matches what set_voltage/set_current already do in Python.")
    elif not styles:
        print("  ROUNDING RULE: undetermined. No probe had a truncation and a nearest")
        print("  neighbour that differ, so nothing here distinguishes the two rules.")
    else:
        print(f"  ROUNDING RULE: inconsistent across probes ({sorted(styles)}) -- read")
        print("  the raw replies above before relying on any write with extra digits.")
    print("=" * 68)


if __name__ == "__main__":
    main()
