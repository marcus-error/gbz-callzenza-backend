from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional
from loguru import logger

from app.core.config import ENV_FILE, get_settings


def read_env_dict() -> Dict[str, str]:
    """Reads key-value pairs directly from the backend .env file."""
    if not ENV_FILE.exists():
        return {}

    env_vars: Dict[str, str] = {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("\"'")
                    env_vars[key] = val
    except Exception as exc:
        logger.error(f"[EnvUtils] Error reading .env file: {exc}")

    return env_vars


def update_env_file(key_values: Dict[str, Any]) -> bool:
    """
    Updates or appends key=value pairs into the .env file,
    updates in-memory os.environ, and invalidates Settings lru_cache.
    """
    if not key_values:
        return True

    # Filter out empty string or masked placeholder values
    cleaned_kvs: Dict[str, str] = {}
    for k, v in key_values.items():
        if v is None:
            continue
        v_str = str(v).strip()
        # Ignore masked dots or placeholder dots
        if "••••" in v_str or (len(v_str) > 0 and all(c == "•" or ord(c) > 127 for c in v_str)):
            continue
        cleaned_kvs[k] = v_str

    if not cleaned_kvs:
        return True

    try:
        lines: list[str] = []
        if ENV_FILE.exists():
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

        updated_keys: set[str] = set()
        new_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            matched_key: Optional[str] = None
            if not stripped.startswith("#") and "=" in stripped:
                line_key = stripped.split("=", 1)[0].strip()
                if line_key in cleaned_kvs:
                    matched_key = line_key

            if matched_key:
                val = cleaned_kvs[matched_key]
                new_lines.append(f"{matched_key}={val}\n")
                updated_keys.add(matched_key)
            else:
                new_lines.append(line)

        # Append any new keys that were not already in the file
        for k, v in cleaned_kvs.items():
            if k not in updated_keys:
                if new_lines and not new_lines[-1].endswith("\n"):
                    new_lines[-1] = new_lines[-1] + "\n"
                new_lines.append(f"{k}={v}\n")

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        # Update running os.environ
        for k, v in cleaned_kvs.items():
            os.environ[k] = v

        # Invalidate Settings cache
        get_settings.cache_clear()
        logger.info(f"[EnvUtils] Synchronized keys {list(cleaned_kvs.keys())} into {ENV_FILE}")
        return True
    except Exception as exc:
        logger.error(f"[EnvUtils] Failed to update .env file: {exc}")
        return False


def sync_provider_to_env(provider: str, config: Dict[str, Any]) -> Dict[str, str]:
    """
    Translates a provider's configuration dictionary into .env variables
    and commits them to the .env file. Returns the dictionary of updated env variables.
    """
    if not config:
        return {}

    env_map: Dict[str, str] = {}
    prov = (provider or "").lower().strip()

    if prov == "sip":
        if "host" in config and config["host"] is not None:
            env_map["SIP_HOST"] = str(config["host"]).strip()
        if "server" in config and config["server"] is not None:
            env_map["SIP_SERVER"] = str(config["server"]).strip()
        if "proxy" in config and config["proxy"] is not None:
            env_map["SIP_PROXY"] = str(config["proxy"]).strip()
        if "domain" in config and config["domain"] is not None:
            env_map["SIP_DOMAIN"] = str(config["domain"]).strip()
        if "username" in config and config["username"] is not None:
            env_map["SIP_USERNAME"] = str(config["username"]).strip()
        if "login" in config and config["login"] is not None:
            env_map["SIP_LOGIN"] = str(config["login"]).strip()
        if "password" in config and config["password"] is not None:
            env_map["SIP_PASSWORD"] = str(config["password"]).strip()
        if "port" in config and config["port"] is not None:
            env_map["SIP_PORT"] = str(config["port"]).strip()
        if "dialing_prefix" in config and config["dialing_prefix"] is not None:
            env_map["SIP_DIALING_PREFIX"] = str(config["dialing_prefix"]).strip()
        if "transport" in config and config["transport"] is not None:
            env_map["SIP_TRANSPORT"] = str(config["transport"]).strip()
        if "register_refresh" in config and config["register_refresh"] is not None:
            env_map["SIP_REGISTER_REFRESH"] = str(config["register_refresh"]).strip()
        if "keep_alive" in config and config["keep_alive"] is not None:
            env_map["SIP_KEEP_ALIVE"] = str(config["keep_alive"]).strip()

    elif prov == "elevenlabs":
        if "api_key" in config and config["api_key"] is not None:
            env_map["ELEVENLABS_API_KEY"] = str(config["api_key"]).strip()
        if "voice_id" in config and config["voice_id"] is not None:
            env_map["ELEVENLABS_VOICE_ID"] = str(config["voice_id"]).strip()

    elif prov == "gemini":
        if "api_key" in config and config["api_key"] is not None:
            env_map["GEMINI_API_KEY"] = str(config["api_key"]).strip()

    elif prov == "twilio":
        if "account_sid" in config and config["account_sid"] is not None:
            env_map["TWILIO_ACCOUNT_SID"] = str(config["account_sid"]).strip()
        if "auth_token" in config and config["auth_token"] is not None:
            env_map["TWILIO_AUTH_TOKEN"] = str(config["auth_token"]).strip()
        if "phone_number" in config and config["phone_number"] is not None:
            env_map["TWILIO_PHONE_NUMBER"] = str(config["phone_number"]).strip()

    elif prov == "sarvam":
        if "api_key" in config and config["api_key"] is not None:
            env_map["SARVAM_API_KEY"] = str(config["api_key"]).strip()

    elif prov in ("whatsapp", "meta"):
        token = config.get("access_token") or config.get("token")
        if token is not None:
            env_map["WHATSAPP_TOKEN"] = str(token).strip()
        if "phone_number_id" in config and config["phone_number_id"] is not None:
            env_map["WHATSAPP_PHONE_NUMBER_ID"] = str(config["phone_number_id"]).strip()
        if "business_account_id" in config and config["business_account_id"] is not None:
            env_map["WHATSAPP_BUSINESS_ACCOUNT_ID"] = str(config["business_account_id"]).strip()
        if "phone_number" in config and config["phone_number"] is not None:
            env_map["WHATSAPP_PHONE_NUMBER"] = str(config["phone_number"]).strip()

    elif prov in ("smtp", "email", "email_config"):
        if "smtp_host" in config and config["smtp_host"] is not None:
            env_map["SMTP_HOST"] = str(config["smtp_host"]).strip()
        if "smtp_port" in config and config["smtp_port"] is not None:
            env_map["SMTP_PORT"] = str(config["smtp_port"]).strip()
        user = config.get("email_address") or config.get("smtp_user")
        if user is not None:
            env_map["SMTP_USER"] = str(user).strip()
        pwd = config.get("password") or config.get("smtp_pass")
        if pwd is not None:
            env_map["SMTP_PASS"] = str(pwd).strip()

    elif prov == "resend":
        if "api_key" in config and config["api_key"] is not None:
            env_map["RESEND_API_KEY"] = str(config["api_key"]).strip()
        if "from_email" in config and config["from_email"] is not None:
            env_map["RESEND_FROM_EMAIL"] = str(config["from_email"]).strip()

    if env_map:
        update_env_file(env_map)

    return env_map


def get_provider_defaults_from_env() -> Dict[str, Dict[str, Any]]:
    """
    Extracts configured integration defaults directly from the .env file and Settings.
    This ensures the frontend auto-fetches real credentials immediately.
    """
    env_vars = read_env_dict()
    settings = get_settings()

    def _get(key: str, fallback: Any = "") -> str:
        val = env_vars.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
        if fallback is not None:
            return str(fallback).strip()
        return ""

    return {
        "sip": {
            "host": "",
            "server": "",
            "proxy": "",
            "domain": "",
            "username": "",
            "login": "",
            "password": "",
            "port": "5060",
            "dialing_prefix": "",
            "transport": "UDP",
            "register_refresh": "300",
            "keep_alive": "15",
        },
        "elevenlabs": {
            "api_key": _get("ELEVENLABS_API_KEY", settings.elevenlabs_api_key),
            "voice_id": _get("ELEVENLABS_VOICE_ID", getattr(settings, "elevenlabs_voice_id", "t8BrjWUT5Z23DLLBzbuY")),
        },
        "gemini": {
            "api_key": _get("GEMINI_API_KEY", settings.gemini_api_key),
        },
        "twilio": {
            "account_sid": _get("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
            "auth_token": _get("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
            "phone_number": _get("TWILIO_PHONE_NUMBER", settings.twilio_phone_number),
        },
        "sarvam": {
            "api_key": _get("SARVAM_API_KEY", settings.sarvam_api_key),
        },
        "whatsapp": {
            "phone_number_id": _get("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id),
            "business_account_id": _get("WHATSAPP_BUSINESS_ACCOUNT_ID", getattr(settings, "whatsapp_business_account_id", "")),
            "phone_number": _get("WHATSAPP_PHONE_NUMBER", getattr(settings, "whatsapp_phone_number", "+1 (555) 159-5909")),
            "token": _get("WHATSAPP_TOKEN", settings.whatsapp_token),
        },
    }
