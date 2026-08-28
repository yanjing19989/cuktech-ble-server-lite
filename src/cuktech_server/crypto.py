from __future__ import annotations
import hmac
import hashlib
import struct
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

def derive_keys(token: bytes, app_rand: bytes, dev_rand: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    if len(token) != 12 or len(app_rand) != 16 or len(dev_rand) != 16:
        raise ValueError("invalid authentication input length")
    out = HKDF(algorithm=hashes.SHA256(), length=64, salt=app_rand + dev_rand, info=b"mible-login-info").derive(token)
    return out[:16], out[16:32], out[32:36], out[36:40]

class SessionCipher:
    def __init__(self, app_key: bytes, dev_key: bytes, app_iv: bytes, dev_iv: bytes):
        self.app_key, self.dev_key, self.app_iv, self.dev_iv = app_key, dev_key, app_iv, dev_iv
        self.send_it = 0
        self.recv_it = 0

    @staticmethod
    def _nonce(iv: bytes, counter: int) -> bytes:
        return iv + b"\x00\x00\x00\x00" + struct.pack("<I", counter)

    def encrypt(self, plaintext: bytes) -> bytes:
        counter = self.send_it
        self.send_it += 1
        wire = AESCCM(self.app_key, tag_length=4).encrypt(self._nonce(self.app_iv, counter), plaintext, None)
        return struct.pack("<H", counter & 0xFFFF) + wire

    def decrypt(self, wire: bytes) -> bytes:
        if len(wire) < 6:
            raise ValueError("ciphertext too short")
        low = struct.unpack_from("<H", wire)[0]
        base = self.recv_it & 0xFFFF0000
        candidates = [base | low, (base - 0x10000) | low, (base + 0x10000) | low]
        counter = min((c for c in candidates if c >= 0), key=lambda c: abs(c - self.recv_it))
        pt = AESCCM(self.dev_key, tag_length=4).decrypt(self._nonce(self.dev_iv, counter), wire[2:], None)
        self.recv_it = counter + 1
        return pt

def verify_device_hmac(dev_key: bytes, dev_rand: bytes, app_rand: bytes, value: bytes) -> bool:
    return hmac.compare_digest(hmac.new(dev_key, dev_rand + app_rand, hashlib.sha256).digest(), value)

def app_hmac(app_key: bytes, app_rand: bytes, dev_rand: bytes) -> bytes:
    return hmac.new(app_key, app_rand + dev_rand, hashlib.sha256).digest()
