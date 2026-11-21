"""M2 tests that need no hardware: edit-buffer conversion, phrases, the KeyLab channel rewrite, SysEx joining."""
from patchmgr.drivers.sub37 import buffer_from
from patchmgr.drivers.take5 import edit_from, is_edit_dump
from patchmgr.midi.hub import Reassembler, match
from patchmgr.midi.phrases import builtins, default_for


def test_take5_edit_from_program():
    prog = b"\xF0\x01\x35\x02\x03\x04" + bytes(range(100)) * 46 + bytes(88) + b"\xF7"
    assert len(prog) == 4695
    e = edit_from(prog)
    assert len(e) == 4693 and is_edit_dump(e) and e[4:-1] == prog[6:-1]


def test_sub37_buffer_from_stored():
    stored = b"\xF0\x04\x0C\x02\x07\x01\x05" + bytes(745) + b"\x00\x00\x00\xF7"
    b = buffer_from(stored)
    assert b[3] == 0x04 and b[4:] == stored[4:]


def test_reassembler_joins_pieces():
    msg = b"\xF0\x01\x35\x03" + bytes(4688) + b"\xF7"
    r = Reassembler()
    out = []
    for k in range(0, len(msg), 1024):
        out += r.feed(msg[k:k + 1024])
    assert out == [msg]


def test_port_names():
    assert match("Take 5 1", "take5") and match("Moog Sub 37 2", "sub37")
    assert match("KeyLab 61 mk3 MIDI 3", "keylab") and not match("KeyLab 61 mk3 DAW 4", "keylab")
    assert match("Deluge 6", "deluge") and not match("MIDIIN2 (Deluge) 7", "deluge")


def test_phrases():
    ph = {p.key: p for p in builtins(145)}
    assert ph["arp"].clock and not ph["single"].clock
    assert sorted(n for t, k, n in ph["pad"].events if k == "on") == [60, 64, 67, 71]
    ons = [t for t, k, n in ph["rolling"].events if k == "on"]
    assert len(ons) == 24 and abs(ons[1] - ons[0] - 60 / 145 / 4) < 1e-9
    assert default_for(["dark", "bass"]) == "pulse" and default_for(["pad"]) == "pad" and default_for([]) == "single"
