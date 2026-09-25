"""Web Push (RFC 8030/8291/8292) zonder externe push-bibliotheek.

VAPID-sleutels worden bij de eerste start aangemaakt en in de database
bewaard, zodat er niets in Portainer ingesteld hoeft te worden.
"""
import base64
import json
import os
import struct
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hmac(key, data):
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def generate_vapid():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    return pem, b64u(pub)


def load_private(pem):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def vapid_jwt(private_key, endpoint, subject):
    u = urlparse(endpoint)
    aud = f"{u.scheme}://{u.netloc}"
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": subject},
                             separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    der = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{b64u(sig)}"


def encrypt(payload: bytes, ua_public_b64: str, auth_b64: str, salt=None, as_private=None):
    """aes128gcm-codering volgens RFC 8291."""
    ua_public = b64u_dec(ua_public_b64)
    auth_secret = b64u_dec(auth_b64)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = as_private.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_private.exchange(ec.ECDH(), ua_key)
    prk_key = _hmac(auth_secret, shared)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hmac(prk_key, key_info + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack(">I", 4096) + bytes([len(as_public)]) + as_public
    return header + ciphertext


def decrypt(body: bytes, ua_private, auth_b64: str):
    """Alleen voor tests: ontsleutelt zoals een browser dat doet."""
    auth_secret = b64u_dec(auth_b64)
    salt = body[:16]
    idlen = body[20]
    as_public = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]
    ua_public = ua_private.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    shared = ua_private.exchange(ec.ECDH(), as_key)
    prk_key = _hmac(auth_secret, shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    return plain.rstrip(b"\x00")[:-1]


def send(sub, data: dict, private_pem: str, public_b64: str, subject: str, ttl=3600):
    """Stuurt één pushbericht. Geeft HTTP-status terug (404/410 = abonnement verlopen)."""
    body = encrypt(json.dumps(data).encode(), sub["p256dh"], sub["auth"])
    jwt = vapid_jwt(load_private(private_pem), sub["endpoint"], subject)
    headers = {
        "TTL": str(ttl),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Urgency": "high",
        "Authorization": f"vapid t={jwt}, k={public_b64}",
    }
    try:
        r = requests.post(sub["endpoint"], data=body, headers=headers, timeout=15)
        return r.status_code
    except requests.RequestException as exc:  # pragma: no cover - netwerk
        print(f"[WorkPortal] Push mislukt: {exc}", flush=True)
        return 0
