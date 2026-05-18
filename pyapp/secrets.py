import base64
import hashlib
import os

ENC_PREFIX = "enc:v1"
ENC_PREFIX_V2 = "enc:v2"


def has_master_key() -> bool:
    raw = os.getenv("MASTER_ENCRYPTION_KEY", "")
    return len(raw) >= 16


def _get_master_secret() -> str:
    raw = os.getenv("MASTER_ENCRYPTION_KEY", "")
    if len(raw) < 16:
        raise ValueError("MASTER_ENCRYPTION_KEY is missing or too short. Set a strong secret in .env.")
    return raw


def _legacy_master_key() -> bytes:
    return hashlib.sha256(_get_master_secret().encode("utf-8")).digest()


def is_encrypted_secret(value: str | None) -> bool:
    return isinstance(value, str) and (
        value.startswith(f"{ENC_PREFIX}:") or value.startswith(f"{ENC_PREFIX_V2}:")
    )


def _derive_v2_key(salt: bytes) -> bytes:
    return hashlib.scrypt(_get_master_secret().encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "cryptography is required for encrypted wallet secret support. "
            "Install dependencies from requirements.txt before using pyapp secrets."
        ) from exc
    return AESGCM


def encrypt_secret(plain_text: str) -> str:
    salt = os.urandom(16)
    key = _derive_v2_key(salt)
    iv = os.urandom(12)
    aes = _aesgcm()(key)
    encrypted = aes.encrypt(iv, plain_text.encode("utf-8"), None)
    cipher_text = encrypted[:-16]
    tag = encrypted[-16:]
    return ":".join(
        [
            ENC_PREFIX_V2,
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(iv).decode("ascii"),
            base64.b64encode(tag).decode("ascii"),
            base64.b64encode(cipher_text).decode("ascii"),
        ]
    )


def decrypt_secret(value: str) -> str:
    if not is_encrypted_secret(value):
        return value

    parts = value.split(":")
    if value.startswith(f"{ENC_PREFIX_V2}:"):
        _, _, salt_b64, iv_b64, tag_b64, data_b64 = parts
        key = _derive_v2_key(base64.b64decode(salt_b64))
    else:
        _, _, iv_b64, tag_b64, data_b64 = parts
        key = _legacy_master_key()

    iv = base64.b64decode(iv_b64)
    tag = base64.b64decode(tag_b64)
    data = base64.b64decode(data_b64)
    aes = _aesgcm()(key)
    decrypted = aes.decrypt(iv, data + tag, None)
    return decrypted.decode("utf-8")
