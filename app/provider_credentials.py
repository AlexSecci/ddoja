"""Local provider credentials: Windows user-bound DPAPI, never dashboard readback."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading

_CREDENTIAL_LOCK = threading.RLock()


def _dpapi(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("Encrypted credential storage requires Windows. Set DEEPSEEK_API_KEY in the service environment instead.")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
    buf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    dst = Blob()
    crypt = ctypes.windll.crypt32
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    ok = (crypt.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)) if decrypt else
          crypt.CryptProtectData(ctypes.byref(src), "DDoJA provider credential", None, None, None, 1, ctypes.byref(dst)))
    if not ok:
        raise RuntimeError("The local credential could not be encrypted or decrypted for this Windows user.")
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(dst.data)


class ProviderCredentials:
    def __init__(self, root):
        self.path = Path(root) / "data/secrets/deepseek.dpapi"
        self.lock = _CREDENTIAL_LOCK

    def get(self):
        with self.lock:
            if self.path.exists():
                try:
                    return _dpapi(base64.b64decode(self.path.read_bytes()), decrypt=True).decode("utf-8")
                except (OSError, ValueError, UnicodeError):
                    raise RuntimeError("The saved DeepSeek credential could not be read.") from None
            return os.environ.get("DEEPSEEK_API_KEY", "").strip()

    def status(self):
        try:
            ready = bool(self.get())
            return {"configured": ready, "source": "encrypted_local" if ready and self.path.exists() else "environment" if ready else None}
        except RuntimeError:
            return {"configured": False, "source": "encrypted_local", "error": "Saved key is unavailable to this service user."}

    def save(self, key):
        if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c.isspace() for c in key.strip()):
            raise ValueError("Enter a valid API key.")
        with self.lock:
            blob = base64.b64encode(_dpapi(key.strip().encode("utf-8")))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_bytes(blob)
            temporary.replace(self.path)
        return self.status()

    def remove(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
        return self.status()
