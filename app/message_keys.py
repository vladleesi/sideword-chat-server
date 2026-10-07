"""Validate the reference client's raw P-256 public keys using OpenSSL."""

from cryptography.hazmat.primitives.asymmetric import ec


def valid_public_key(value: bytes) -> bool:
    """Accept only a valid uncompressed SEC1 point on P-256."""
    if len(value) != 65 or value[0] != 4:
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), value)
    except ValueError:
        return False
    return True
