# Centralized Regional Language Configuration for CallZenza

LANGUAGES = {
    "Auto Detect": {"code": "auto", "locale": "en-US", "name": "Auto Detect"},
    "English": {"code": "en", "locale": "en-US", "name": "English"},
    "Tamil": {"code": "ta", "locale": "ta-IN", "name": "Tamil"},
    "Telugu": {"code": "te", "locale": "te-IN", "name": "Telugu"},
    "Malayalam": {"code": "ml", "locale": "ml-IN", "name": "Malayalam"},
    "Kannada": {"code": "kn", "locale": "kn-IN", "name": "Kannada"},
    "Hindi": {"code": "hi", "locale": "hi-IN", "name": "Hindi"},
    "Bengali": {"code": "bn", "locale": "bn-IN", "name": "Bengali"},
    "Marathi": {"code": "mr", "locale": "mr-IN", "name": "Marathi"},
    "Gujarati": {"code": "gu", "locale": "gu-IN", "name": "Gujarati"},
    "Punjabi": {"code": "pa", "locale": "pa-IN", "name": "Punjabi"},
    "Urdu": {"code": "ur", "locale": "ur-IN", "name": "Urdu"},
    "Odia": {"code": "or", "locale": "or-IN", "name": "Odia"},
}

def get_locale_by_code(code: str) -> str:
    """Return Twilio locale string (e.g. 'ta-IN') for a given language code (e.g. 'ta')."""
    if not code:
        return "en-US"
    for lang, config in LANGUAGES.items():
        if config["code"] == code or config["locale"] == code:
            return config["locale"]
    return "en-US"

def get_language_name_by_code(code: str) -> str:
    """Return language name for a given code."""
    if not code:
        return "English"
    for lang, config in LANGUAGES.items():
        if config["code"] == code or config["locale"] == code:
            return config["name"]
    return "English"
