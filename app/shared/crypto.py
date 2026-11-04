import os
from pathlib import Path

from cryptography.fernet import Fernet


def load_or_create_master_key(secrets_dir: Path) -> bytes:
    """Load {secrets_dir}/master.key, generating it (0600) if missing. Never logged."""
    path = secrets_dir / "master.key"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class SecretCipher:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode())

    def decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(token).decode()
