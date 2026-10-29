"""
Centralized Language Service for CallZenza.

Provides robust language detection, Tanglish recognition, conversation state management,
and prompt formatting for Sarvam AI voice calls.
"""
from __future__ import annotations

import re
from typing import Optional, Dict, Any, Tuple
from loguru import logger

from app.core.languages import LANGUAGES, get_language_name_by_code, get_locale_by_code


# Common Tanglish (Tamil written in Latin script) keywords and conversational markers
# Note: Universal words like 'hello', 'hi', 'sir', 'madam' are excluded here so they don't misclassify English sentences.
TANGLISH_KEYWORDS = {
    # Greetings & Courtesy
    "vanakkam", "vannakam", "வணக்கம்", "nandri", "நன்றி",
    # Pronouns & Pointers
    "enakku", "enaku", "எனக்கு", "unga", "ungals", "உங்க", "namma", "naan", "naanga",
    "indha", "intha", "இந்த", "andha", "antha", "அந்த", "idhu", "ithu", "இது", "adhu", "athhu",
    # Common Verbs & Actions
    "therinjukanum", "therinjikalam", "தெரிந்துகொள்ள", "solren", "sollunga", "சொல்லுங்க",
    "kettukren", "pannanum", "panren", "panni", "varugirean", "paarpom", "teriyum",
    "venum", "venom", "வேண்டும்", "irukku", "irukeenga", "இருக்கீங்க", "irukanga",
    # Question Words & Fillers
    "epdi", "eppadi", "எப்படி", "yena", "enna", "என்ன", "yean", "yaen", "ஏன்",
    "evlo", "evvalavu", "எவ்வளவு", "eppo", "எப்போது", "enga", "எங்கே",
    "sari", "seri", "சரி", "aama", "aamanga", "illai", "illa", "illanga",
    "pathi", "bathi", "பற்றி", "aachu", "aacha", "da", "pa", "nga", "enuga",
}

# Regex to detect Tamil Unicode characters (U+0B80 to U+0BFF)
TAMIL_UNICODE_PATTERN = re.compile(r'[\u0B80-\u0BFF]')

# Regex patterns to detect script ranges across Indian languages
HINDI_UNICODE_PATTERN = re.compile(r'[\u0900-\u097F]')
TELUGU_UNICODE_PATTERN = re.compile(r'[\u0C00-\u0C7F]')
KANNADA_UNICODE_PATTERN = re.compile(r'[\u0C80-\u0CFF]')
MALAYALAM_UNICODE_PATTERN = re.compile(r'[\u0D00-\u0D7F]')
BENGALI_UNICODE_PATTERN = re.compile(r'[\u0980-\u09FF]')

# Centralized System Prompt Instruction for Multilingual Voice Agents
MULTILINGUAL_SYSTEM_PROMPT = """
CRITICAL MULTILINGUAL & LANGUAGE CONSISTENCY INSTRUCTIONS:
You are a multilingual voice agent.
1. Always respond in the language currently being used by the customer ({lang_name}).
2. If the customer speaks Tamil or Tanglish (Tamil words written using English/Latin characters, e.g. "Vanakkam", "enakku indha service pathi therinjukanum"), you MUST respond naturally in Tamil using Tamil script.
3. If the customer speaks Hindi (e.g., "मुझे अपॉइंटमेंट चाहिए"), respond naturally in Hindi using Devanagari script.
4. If the customer speaks English, respond in English.
5. If the customer uses mixed language (code-switching, e.g. "Vanakkam sir, loan details venum"), recognize the primary conversation intent and respond in that language.
6. If the campaign/script has a configured language ({lang_name}), use that language as the initial conversation language.
7. Do NOT translate responses into English unless the customer switched to English.
8. Do NOT switch languages unnecessarily. Maintain the established conversation language ({lang_name}) throughout the call unless the customer explicitly switches language.
"""


def get_time_of_day_salutation(language: str = "en") -> str:
    """Returns time-of-day salutation based on current local hour."""
    import datetime
    now = datetime.datetime.now()
    hour = now.hour
    lang = (language or "en").lower()

    if 5 <= hour < 12:
        if lang == "ta":
            return "காலை வணக்கம்"
        elif lang == "hi":
            return "शुभ प्रभात"
        else:
            return "Good Morning"
    elif 12 <= hour < 17:
        if lang == "ta":
            return "மதிய வணக்கம்"
        elif lang == "hi":
            return "शुभ दोपहर"
        else:
            return "Good Afternoon"
    else:
        if lang == "ta":
            return "மாலை வணக்கம்"
        elif lang == "hi":
            return "शुभ संध्या"
        else:
            return "Good Evening"


def generate_time_based_greeting(
    agent_name: str | None = None,
    business_name: str | None = None,
    language: str = "en",
    caller_name: str | None = None,
    custom_script_greeting: str | None = None,
    call_direction: str = "outbound",
) -> str:
    """
    Generates a centralized dynamic time-of-day initial greeting for the AI Voice Agent.
    Adapts greeting based on call_direction (inbound receptionist vs outbound outreach).
    """
    salutation = get_time_of_day_salutation(language)
    lang = (language or "en").lower()
    direction = (call_direction or "outbound").lower()

    if custom_script_greeting and custom_script_greeting.strip():
        return custom_script_greeting.strip()

    agent_title = agent_name or "your AI assistant"
    biz_title = business_name or "our company"

    if direction == "inbound":
        if lang == "ta":
            greeting = f"{salutation}, அழைக்க நன்றி! நான் {agent_title}."
            if caller_name:
                greeting = f"{salutation} {caller_name}, அழைக்க நன்றி! நான் {agent_title}."
            greeting += " இன்று உங்களுக்கு எவ்வாறு உதவ முடியும்?"
            return greeting
        elif lang == "hi":
            greeting = f"{salutation} और कॉल करने के लिए धन्यवाद, मैं {agent_title} हूँ।"
            if caller_name:
                greeting = f"{salutation} {caller_name} और कॉल करने के लिए धन्यवाद, मैं {agent_title} हूँ।"
            greeting += " आज मैं आपकी क्या मदद कर सकता हूँ?"
            return greeting
        else:
            if caller_name:
                greeting = f"{salutation} {caller_name} and Thank You for Calling, I am {agent_title}. How can I help you today?"
            else:
                greeting = f"{salutation} and Thank You for Calling, I am {agent_title}. How can I help you today?"
            return greeting
    else:
        # Outbound call greeting
        if lang == "ta":
            if caller_name:
                greeting = f"{salutation} {caller_name}! நான் {agent_title}, {biz_title}-ல் இருந்து அழைக்கிறேன். இன்று உங்களுக்கு எவ்வாறு உதவ முடியும்?"
            else:
                greeting = f"{salutation}! நான் {agent_title}, {biz_title}-ல் இருந்து அழைக்கிறேன். இன்று உங்களுக்கு எவ்வாறு உதவ முடியும்?"
            return greeting
        elif lang == "hi":
            if caller_name:
                greeting = f"{salutation} {caller_name}! मैं {agent_title}, {biz_title} से बात कर रहा हूँ। आज मैं आपकी क्या सहायता कर सकता हूँ?"
            else:
                greeting = f"{salutation}! मैं {agent_title}, {biz_title} से बात कर रहा हूँ। आज मैं आपकी क्या सहायता कर सकता हूँ?"
            return greeting
        else:
            if caller_name:
                greeting = f"{salutation} {caller_name}! Hi, this is {agent_title} from {biz_title}. How are you doing today?"
            else:
                greeting = f"{salutation}! Hi, this is {agent_title} from {biz_title}. How can I help you today?"
            return greeting


def is_tamil_unicode(text: str) -> bool:
    """Check if text contains Tamil Unicode characters."""
    if not text:
        return False
    return bool(TAMIL_UNICODE_PATTERN.search(text))


def detect_script_unicode(text: str) -> Optional[str]:
    """Detects primary non-English script Unicode range in user text."""
    if not text:
        return None
    if TAMIL_UNICODE_PATTERN.search(text):
        return "ta"
    if HINDI_UNICODE_PATTERN.search(text):
        return "hi"
    if TELUGU_UNICODE_PATTERN.search(text):
        return "te"
    if KANNADA_UNICODE_PATTERN.search(text):
        return "kn"
    if MALAYALAM_UNICODE_PATTERN.search(text):
        return "ml"
    if BENGALI_UNICODE_PATTERN.search(text):
        return "bn"
    return None


def is_tanglish_or_tamil(text: str) -> bool:
    """
    Robust check if text is Tamil (Unicode) or Tanglish (Tamil in Latin script).
    Returns True if Tamil Unicode or Tanglish keywords/patterns are detected.
    """
    if not text or not text.strip():
        return False

    if is_tamil_unicode(text):
        return True

    words = re.findall(r'\b[a-zA-Z]+\b', text.lower())
    if not words:
        return False

    tanglish_count = sum(1 for word in words if word in TANGLISH_KEYWORDS)
    
    # Single strong Tanglish word or multiple words
    if tanglish_count >= 1:
        return True

    # Multi-word Tanglish patterns
    lowered = text.lower()
    tanglish_phrases = [
        "epdi irukeenga", "epdi irukinga", "vanakkam sir", "vanakkam madam",
        "service pathi", "details venum", "emi evlo", "price evlo",
        "product pathi", "solla mudiyuma", "call panni", "konjam sollunga",
        "aduthatas enna", "sari aduthatas", "konjam solren"
    ]
    if any(phrase in lowered for phrase in tanglish_phrases):
        return True

    return False


def detect_script_language(script_text: str, default_lang: str = "en") -> str:
    """
    Priority 1 Helper: Detects explicit language of a campaign script.
    Returns detected script language code or default_lang.
    """
    if not script_text or not script_text.strip():
        return default_lang

    unicode_lang = detect_script_unicode(script_text)
    if unicode_lang:
        logger.info(f"[LANGUAGE] Script language detected via Unicode: {unicode_lang}")
        return unicode_lang

    if is_tanglish_or_tamil(script_text):
        logger.info("[LANGUAGE] Script language detected: ta (Tamil/Tanglish script)")
        return "ta"

    # Check for English indicators
    words = re.findall(r'\b[a-zA-Z]+\b', script_text.lower())
    english_common = {"hello", "hi", "calling", "company", "today", "solar", "agent", "service", "help"}
    if any(w in words for w in english_common):
        logger.info("[LANGUAGE] Script language detected: en (English script)")
        return "en"

    return default_lang


# Deterministic language switch mapping with natural acknowledgements
LANGUAGE_SWITCH_RULES = [
    # Tamil
    (
        r'(?i)(?:(?:(?:no|please|can\s+you(?:\s+please)?|you\s+can)\s*[,.]?\s*)?(?:continue|talk|speak|switch|change|converse|reply|respond|pesu|pesunga)\s+(?:in\s+|to\s+(?:me\s+in\s+|me\s+)?|with\s+me\s+in\s+|to\s+)?tamil|tamil\s*(?:please|pes|pesu|pesunga|la\s*pesunga|la\s*pesu|la\s*sollu|le\s*pesu|il\s*pesavum|il\s*pesunga|tamil|\s*)*$|^(?:tamil[\s,.]*)+$|தமிழில்|தமிழ்(?:ல\s*பேசுங்க|ில்\s*பேசுங்கள்|ில்\s*தொடரவும்|ல\s*பேசு|ல\s*பேச|்)?|எனக்கு\s*தமிழ்(?:ல|ில்)\s*(?:பேச|பேசுங்க)|tanglish)',
        "ta",
        "சரி, தமிழில் தொடரலாம்."
    ),
    # English
    (
        r'(?i)(?:(?:(?:no|please|can\s+you(?:\s+please)?|you\s+can)\s*[,.]?\s*)?(?:continue|talk|speak|switch|change|converse|reply|respond)\s+(?:in\s+|to\s+(?:me\s+in\s+|me\s+)?|with\s+me\s+in\s+|to\s+)?english|english\s*(?:please|la\s*pesunga|mein\s*bolo|me\s*baat\s*karo|\s*)*$|^(?:english[\s,.]*)+$|ஆங்கிலத்தில்|ஆங்கிலம்|अंग्रेजी)',
        "en",
        "Sure, continuing in English."
    ),
    # Hindi
    (
        r'(?i)(?:(?:(?:no|please|can\s+you(?:\s+please)?|you\s+can)\s*[,.]?\s*)?(?:continue|talk|speak|switch|change|converse|reply|respond)\s+(?:in\s+|to\s+(?:me\s+in\s+|me\s+)?|with\s+me\s+in\s+|to\s+)?hindi|hindi\s*(?:please|mein\s*baat\s*karo|me\s*bolo|mein\s*boliye|\s*)*$|^(?:hindi[\s,.]*)+$|हिंदी|हिन्दी|இந்தி)',
        "hi",
        "जी हाँ, हिंदी में जारी रखते हैं।"
    ),
    # Telugu
    (
        r'(?i)(?:(?:(?:no|please|can\s+you(?:\s+please)?|you\s+can)\s*[,.]?\s*)?(?:continue|talk|speak|switch|change|converse|reply|respond)\s+(?:in\s+|to\s+(?:me\s+in\s+|me\s+)?|with\s+me\s+in\s+|to\s+)?telugu|telugu\s*(?:please|lo\s*matladandi|lo\s*cheppandi|\s*)*$|^(?:telugu[\s,.]*)+$|తెలుగు)',
        "te",
        "సరే, తెలుగులో కొనసాగిద్దాం."
    ),
    # Malayalam
    (
        r'(?i)(?:(?:(?:no|please|can\s+you(?:\s+please)?|you\s+can)\s*[,.]?\s*)?(?:continue|talk|speak|switch|change|converse|reply|respond)\s+(?:in\s+|to\s+(?:me\s+in\s+|me\s+)?|with\s+me\s+in\s+|to\s+)?malayalam|malayalam\s*(?:please|parayu|il\s*parayoo|\s*)*$|^(?:malayalam[\s,.]*)+$|മലയാളം)',
        "ml",
        "തീർച്ചയായും, മലയാളത്തിൽ തുടരാം."
    ),
    # Kannada
    (
        r'(?i)(?:(?:(?:no|please|can\s+you(?:\s+please)?|you\s+can)\s*[,.]?\s*)?(?:continue|talk|speak|switch|change|converse|reply|respond)\s+(?:in\s+|to\s+(?:me\s+in\s+|me\s+)?|with\s+me\s+in\s+|to\s+)?kannada|kannada\s*(?:please|dalli\s*mathadi|alli\s*heli|\s*)*$|^(?:kannada[\s,.]*)+$|கன்னடம்|கன்னட|ಕನ್ನಡ)',
        "kn",
        "ಖಂಡಿತ, ಕನ್ನಡದಲ್ಲಿ ಮುಂದುವರಿಸೋಣ."
    ),
]


def detect_explicit_language_switch_request(user_text: str) -> Optional[Tuple[str, str]]:
    """
    Detects if the user explicitly requested a language switch BEFORE performing RAG.
    Returns: (target_language_code, acknowledgement_message) or None.
    """
    if not user_text or not user_text.strip():
        return None

    clean = user_text.strip()
    for pattern, lang_code, ack_message in LANGUAGE_SWITCH_RULES:
        if re.search(pattern, clean):
            logger.info(f"[LANGUAGE] Explicit language switch detected: {lang_code} from input: '{clean}'")
            return lang_code, ack_message

    return None


def detect_speech_language(
    user_text: str,
    current_session_lang: str = "en",
    script_lang: str = "en"
) -> str:
    """
    Core Language Selection Strategy (Priority 1 -> 2 -> 3 -> 4):
    
    Priority 1: Explicit language switch request.
    Priority 2: Detect language from customer's latest speech/transcript (Unicode script or Tanglish).
    Priority 3: Maintain previously established conversation language (current_session_lang).
    Priority 4: Fallback to English ('en').
    """
    if not user_text or not user_text.strip():
        return current_session_lang or script_lang or "en"

    # Priority 1: Check explicit switch
    explicit = detect_explicit_language_switch_request(user_text)
    if explicit:
        return explicit[0]

    # Priority 2: Check Unicode script match (Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali)
    unicode_lang = detect_script_unicode(user_text)
    if unicode_lang:
        logger.info(f"[LANGUAGE] Customer speech matched Unicode script: {unicode_lang} (Utterance: '{user_text}')")
        return unicode_lang

    if is_tanglish_or_tamil(user_text):
        logger.info(f"[LANGUAGE] Customer speech classified as Tamil/Tanglish: ta (Utterance: '{user_text}')")
        return "ta"

    lowered_text = user_text.strip().lower()

    # If already in a non-English session, do NOT flip to English on short loan words, numbers, or short answers
    if current_session_lang and current_session_lang != "en":
        words_in_text = re.findall(r'\b[a-zA-Z]+\b', lowered_text)
        # Check if user explicitly asked for English in full sentence
        if any(p in lowered_text for p in ["speak in english", "talk in english", "english please", "switch to english", "can you speak english", "i want english"]):
            logger.info(f"[LANGUAGE] Customer explicitly switched to English: en (Utterance: '{user_text}')")
            return "en"
        # Short words like 'appointment', 'home', 'single family', 'owner', 'yes', 'no', '631001', 'okay', etc. maintain current language
        if len(words_in_text) <= 5:
            logger.info(f"[LANGUAGE] Short input '{user_text}' in established {current_session_lang} session -> Maintaining {current_session_lang}")
            return current_session_lang

    # Check if utterance is purely English
    english_words = {"hello", "hi", "hey", "good morning", "good afternoon", "good evening", "thank you", "thanks", "what", "where", "how", "why", "who", "please", "would like", "want to know", "tell me", "appointment", "tomorrow", "today", "schedule", "reschedule", "cancel", "doctor", "service", "yes", "no", "okay", "sure"}
    words_in_text = set(re.findall(r'\b[a-zA-Z]+\b', lowered_text))

    if words_in_text.intersection(english_words) and not is_tanglish_or_tamil(user_text):
        if current_session_lang in ("ta", "hi", "te", "ml", "kn") and len(words_in_text) <= 3:
            logger.info(f"[LANGUAGE] Short loan word '{user_text}' in {current_session_lang} conversation -> Maintaining current_session_lang: {current_session_lang}")
            return current_session_lang

        logger.info(f"[LANGUAGE] Customer speech classified as English: en (Utterance: '{user_text}')")
        return "en"

    # Priority 3: Maintain previously established conversation language
    if current_session_lang in ("ta", "en", "hi", "te", "ml", "kn", "bn"):
        logger.info(f"[LANGUAGE] Maintaining established conversation language: {current_session_lang}")
        return current_session_lang

    # Priority 4: Fallback to script language or English
    final_fallback = script_lang or "en"
    logger.info(f"[LANGUAGE] Fallback language selected: {final_fallback}")
    return final_fallback


def format_multilingual_prompt(system_prompt: str, target_lang: str = "en") -> str:
    """Injects centralized multilingual consistency guidelines into any system prompt."""
    lang_name = get_language_name_by_code(target_lang)
    multilingual_rules = MULTILINGUAL_SYSTEM_PROMPT.format(lang_name=lang_name)
    return f"{system_prompt}\n\n{multilingual_rules}"


