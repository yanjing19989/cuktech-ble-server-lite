import sys, pathlib, pytest
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))
from cuktech_server.protocol import *
from cuktech_server.crypto import derive_keys, SessionCipher

def test_crypto_roundtrip_and_wrap():
    keys = derive_keys(bytes(12), bytes(range(16)), bytes(range(16,32)))
    c = SessionCipher(keys[1], keys[1], keys[3], keys[3])
    wire = c.encrypt(b"hello")
    assert c.decrypt(wire) == b"hello"

def test_bad_lengths():
    with pytest.raises(ValueError): parse_port(1, b"x")
    with pytest.raises(ValueError): parse_frame_header(b"\x00\x00\x00\x00\x65\x00")
