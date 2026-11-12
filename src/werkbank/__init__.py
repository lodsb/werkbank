"""Serial drivers and port discovery for bench instruments.

    from werkbank.instruments import find_korad, find_owon
    from werkbank.korad_ka3005 import KoradKA3005
    from werkbank.owon_xdm1041 import ...

Drivers only. Safety limits belong to the project that knows what is
connected to the terminals -- see README.
"""
__version__ = "0.1.0"
