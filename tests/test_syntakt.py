"""Syntakt driver pieces that need no hardware."""
import io
import json
import random
import struct
import zipfile

import pytest

from patchmgr.drivers.syntakt import decode, encode, parse_list, payload_of


def test_encode_roundtrip_and_bit_order():
    for n in (0, 1, 6, 7, 8, 15, 300):
        d = bytes(random.getrandbits(8) for _ in range(n))
        assert decode(encode(d)) == d
    assert encode(b"\x80\x00") == b"\x40\x00\x00"           # the first byte's top bit is 0x40, as Elektroid has it


def test_payload_of():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("manifest.json", json.dumps({"FileType": "Sound", "Payload": "BLOMP"}))
        z.writestr("BLOMP", b"\xAC\x11\xD3\x03rest")
    assert payload_of(b.getvalue()) == ("BLOMP", b"\xAC\x11\xD3\x03rest")
    b2 = io.BytesIO()
    with zipfile.ZipFile(b2, "w") as z:
        z.writestr("manifest.json", json.dumps({"FileType": "Project", "Payload": "P"}))
        z.writestr("P", b"x")
    with pytest.raises(ValueError):
        payload_of(b2.getvalue())


def test_parse_list():
    body = bytearray(18)
    body += b"Blomp\0" + bytes([0, 2]) + struct.pack(">IIHBB", 2, 198, 0, 1, 1)
    body += b"Kick\0" + bytes([0, 2]) + struct.pack(">IIHBB", 3, 205, 0, 1, 0)
    assert [(e["name"], e["slot"]) for e in parse_list(bytes(body))] == [("Blomp", 2), ("Kick", 3)]
