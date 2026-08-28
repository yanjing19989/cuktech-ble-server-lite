import sys, pathlib, pytest
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))
from cuktech_server.protocol import *
from cuktech_server.crypto import derive_keys, SessionCipher

def test_port_zero_and_protocols():
    p = bytes(8) + bytes([0, 99, 20, 90])
    assert parse_port(1, p)["protocol"] == "idle"
    for code, name in ((1,"5V"),(3,"QC"),(7,"PD"),(8,"PPS"),(10,"UFCS")):
        assert parse_port(1, bytes(8)+bytes([1,code,20,90]))["protocol"] == name
    assert parse_port(1, bytes(8)+bytes([1,99,20,90]))["protocol"] == "Unknown"
    assert parse_port(1, bytes(8)+bytes([1,99,23,183]))["protocol"] == "PPS"
    assert parse_port(3, bytes(8)+bytes([1,99,10,90]))["protocol"] == "QC"
    assert parse_port(3, bytes(8)+bytes([1,99,10,50]))["protocol"] == "5V"
    assert parse_port(4, bytes(8)+bytes([1,99,10,90]))["protocol"] == "QC"

def test_miot_layout():
    get = build_miot(1, 5)
    assert get[:9] == bytes([12,0x20,1,0,2,1,2,5,0])
    assert get[9:11] == bytes([0x10,0x01])
    assert build_miot(2, 16, 0x12345678)[9:11] == bytes([4,0x50])

def test_crypto_roundtrip_and_wrap():
    keys = derive_keys(bytes(12), bytes(range(16)), bytes(range(16,32)))
    c = SessionCipher(keys[1], keys[1], keys[3], keys[3])
    wire = c.encrypt(b"hello")
    assert c.decrypt(wire) == b"hello"

def test_bad_lengths():
    with pytest.raises(ValueError): parse_port(1, b"x")
    with pytest.raises(ValueError): parse_frame_header(b"\x00\x00\x00\x00\x65\x00")
