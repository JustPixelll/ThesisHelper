"""Local settings. API secrets never enter JSON settings, responses, or logs."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

MODELS = [
    {"id": "gpt-6-astra", "label": "GPT-6 Astra — highest capability"},
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol — lower cost"},
    {"id": "gpt-6.1-terra", "label": "GPT-6.1 Terra — fast writing"},
    {"id": "gpt-6-luna", "label": "GPT-6 Luna — economical"},
]
DEFAULTS: dict[str, Any] = {
    "model": "gpt-6-astra",
    "reasoning_effort": "high",
    "reasoning_mode": "standard",
    "language": "en",
    "citation_style": "apa",
    "max_output_tokens": 12000,
}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _protect_windows(value: bytes, decrypt: bool = False) -> bytes:
    """Use the current Windows user's DPAPI, with interactive prompts disabled."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError("Windows could not access the protected API key. Enter it again in Settings.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


class SettingsStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "settings.json"
        self.key_path = self.data_dir / "key.dpapi"
        self.lock = threading.RLock()
        self.values = dict(DEFAULTS)
        self.key = ""
        self.storage = "none"
        self.warning = ""
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.values.update(self._validate({k: v for k, v in saved.items() if k in DEFAULTS}))
            except (ValueError, OSError, TypeError, AttributeError):
                self.warning = "Settings could not be read; safe defaults are active."
        if os.name == "nt" and self.key_path.exists():
            try:
                self.key = _protect_windows(self.key_path.read_bytes(), decrypt=True).decode("utf-8")
                self.storage = "windows_protected"
            except (OSError, UnicodeError):
                self.warning = "The saved key could not be unlocked. Enter it again in Settings."
        environment_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if environment_key and not self.key:
            self.key = environment_key
            self.storage = "environment"

    @staticmethod
    def _validate(patch: dict) -> dict:
        clean = {}
        for name, value in patch.items():
            if name not in DEFAULTS:
                continue
            if name == "model" and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", value)):
                raise ValueError("Enter a valid OpenAI model ID.")
            if name == "reasoning_effort" and value not in {"low", "medium", "high", "xhigh", "max"}:
                raise ValueError("Choose a supported reasoning effort.")
            if name == "reasoning_mode" and value not in {"standard", "pro"}:
                raise ValueError("Choose standard or pro reasoning mode.")
            if name == "language" and value not in {"en", "de"}:
                raise ValueError("Choose English or German.")
            if name == "citation_style" and value not in {"apa", "harvard"}:
                raise ValueError("Choose APA or Harvard.")
            if name == "max_output_tokens" and (type(value) is not int or not 2000 <= value <= 32000):
                raise ValueError("Output budget must be between 2,000 and 32,000 tokens per model call.")
            clean[name] = value
        return clean

    def public(self) -> dict:
        with self.lock:
            return {**self.values, "api_key_set": bool(self.key), "key_storage": self.storage,
                    "remember_key": self.storage == "windows_protected",
                    "can_remember_key": os.name == "nt", "warning": self.warning,
                    "embedding_model": "text-embedding-3-large", "embedding_dimensions": 1024}

    def snapshot(self) -> tuple[dict, str]:
        with self.lock:
            return dict(self.values), self.key

    def update(self, patch: dict) -> dict:
        values = self._validate(patch)
        with self.lock:
            key = patch.get("api_key")
            if key is not None:
                if not isinstance(key, str) or len(key) > 1000 or any(c.isspace() for c in key.strip()):
                    raise ValueError("The API key must be a single value without spaces.")
                key = key.strip()
                remember = bool(patch.get("remember_key", self.storage == "windows_protected"))
                if key and remember and os.name == "nt":
                    encrypted = _protect_windows(key.encode("utf-8"))
                    temporary = self.key_path.with_suffix(".tmp")
                    temporary.write_bytes(encrypted)
                    os.replace(temporary, self.key_path)
                    storage = "windows_protected"
                else:
                    self.key_path.unlink(missing_ok=True)
                    storage = "session" if key else "none"
                self.key, self.storage = key, storage
                self.warning = ""
                if key and remember and os.name != "nt":
                    self.warning = "The key is kept for this session only. Protected persistence is available on Windows."
            elif "remember_key" in patch and self.key:
                # Changing only persistence still applies to the existing secret.
                return self.update({**patch, "api_key": self.key})
            self.values.update(values)
            atomic_json(self.path, self.values)
            return self.public()

    @property
    def installation_id(self) -> str:
        return hashlib.sha256(str(self.data_dir.resolve()).encode("utf-8")).hexdigest()[:24]
