"""
Comprehensive Test Suite for Language Consistency in CallZenza (Sarvam AI).

Validates all 10 scenario test cases specified in the objective:
- TEST 1: Script = Tamil, Customer = Tamil -> Tamil
- TEST 2: Script = English, Customer = English -> English
- TEST 3: Script = Tamil, Customer = Tanglish -> Tamil
- TEST 4: Script = English, Customer = Tamil -> Tamil (Customer priority)
- TEST 5: Customer starts Tamil -> continues Tamil -> No switching
- TEST 6: Customer starts English -> continues English -> No switching
- TEST 7: Customer starts Tamil -> explicitly switches to English -> Switch to English
- TEST 8: Mixed Tamil + English sentence -> Tamil context preserved
- TEST 9: Empty/Ambiguous speech -> Configured campaign/script language used
- TEST 10: Existing English campaign -> English behavior remains functional
"""
import pytest
from app.services.language_service import (
    detect_script_language,
    detect_speech_language,
    is_tanglish_or_tamil,
    is_tamil_unicode,
    format_multilingual_prompt,
)


def test_scenario_1_tamil_script_tamil_customer():
    script_text = "வணக்கம், நான் CallZenza சார்பாக பேசுகிறேன். உங்களுக்கு ஒரு சிறிய தகவலை தெரிவிக்க அழைத்துள்ளேன்."
    customer_speech = "வணக்கம், எனக்கு இந்த சேவை பற்றி தெரிந்து கொள்ள வேண்டும்."
    
    script_lang = detect_script_language(script_text)
    assert script_lang == "ta", "Tamil script must be recognized as 'ta'"
    
    detected_lang = detect_speech_language(customer_speech, current_session_lang=script_lang, script_lang=script_lang)
    assert detected_lang == "ta", "Tamil speech must result in 'ta' conversation language"


def test_scenario_2_english_script_english_customer():
    script_text = "Hello, I am calling from ABC Company. How are you today?"
    customer_speech = "Hello, I would like to know more about the service."
    
    script_lang = detect_script_language(script_text)
    assert script_lang == "en", "English script must be recognized as 'en'"
    
    detected_lang = detect_speech_language(customer_speech, current_session_lang=script_lang, script_lang=script_lang)
    assert detected_lang == "en", "English speech must result in 'en' conversation language"


def test_scenario_3_tamil_script_tanglish_customer():
    script_text = "வணக்கம்! உங்களுக்கு எவ்வாறு உதவ முடியும்?"
    customer_speech = "Vanakkam, enakku indha service pathi therinjukanum."
    
    script_lang = detect_script_language(script_text)
    assert script_lang == "ta"
    
    assert is_tanglish_or_tamil(customer_speech) is True, "Tanglish text must be recognized as Tamil"
    detected_lang = detect_speech_language(customer_speech, current_session_lang=script_lang, script_lang=script_lang)
    assert detected_lang == "ta", "Tanglish speech must be recognized as Tamil ('ta')"


def test_scenario_4_english_script_tamil_customer():
    script_text = "Hello, I am calling from Solar Inc. Can I ask you a few questions?"
    customer_speech = "வணக்கம், எனக்கு இந்த சேவை வேண்டும்."
    
    script_lang = detect_script_language(script_text)
    assert script_lang == "en"
    
    detected_lang = detect_speech_language(customer_speech, current_session_lang="en", script_lang=script_lang)
    assert detected_lang == "ta", "Customer speaking Tamil must override English script to 'ta'"


def test_scenario_5_tamil_continuation_no_switching():
    # Customer starts Tamil -> continues Tamil
    turn1_speech = "வணக்கம், எப்படி இருக்கீங்க?"
    lang1 = detect_speech_language(turn1_speech, current_session_lang="ta", script_lang="ta")
    assert lang1 == "ta"
    
    turn2_speech = "எனக்கு இந்த service பற்றி சொல்லுங்க."
    lang2 = detect_speech_language(turn2_speech, current_session_lang=lang1, script_lang="ta")
    assert lang2 == "ta"
    
    turn3_speech = "இதுக்கு என்ன price?"
    lang3 = detect_speech_language(turn3_speech, current_session_lang=lang2, script_lang="ta")
    assert lang3 == "ta", "Asking 'price?' in an ongoing Tamil conversation must stay in 'ta'"


def test_scenario_6_english_continuation_no_switching():
    turn1_speech = "Hello, good morning!"
    lang1 = detect_speech_language(turn1_speech, current_session_lang="en", script_lang="en")
    assert lang1 == "en"
    
    turn2_speech = "Tell me about the monthly plans."
    lang2 = detect_speech_language(turn2_speech, current_session_lang=lang1, script_lang="en")
    assert lang2 == "en", "Ongoing English conversation must stay in 'en'"


def test_scenario_7_explicit_language_switch():
    turn1_speech = "Vanakkam, epdi irukeenga?"
    lang1 = detect_speech_language(turn1_speech, current_session_lang="ta", script_lang="ta")
    assert lang1 == "ta"
    
    turn2_speech = "Can we speak in English please?"
    lang2 = detect_speech_language(turn2_speech, current_session_lang=lang1, script_lang="ta")
    assert lang2 == "en", "Explicit customer request must switch conversation language to 'en'"


def test_scenario_8_mixed_tamil_english_sentence():
    mixed_speech = "Vanakkam sir, enakku loan details venum. Monthly EMI evlo?"
    
    assert is_tanglish_or_tamil(mixed_speech) is True
    detected_lang = detect_speech_language(mixed_speech, current_session_lang="ta", script_lang="ta")
    assert detected_lang == "ta", "Mixed Tanglish + English loan query must be classified as 'ta'"


def test_scenario_9_empty_or_failed_detection_uses_script_language():
    empty_speech = ""
    detected_lang_ta = detect_speech_language(empty_speech, current_session_lang="", script_lang="ta")
    assert detected_lang_ta == "ta", "Failed detection must fall back to script language 'ta'"
    
    detected_lang_en = detect_speech_language(empty_speech, current_session_lang="", script_lang="en")
    assert detected_lang_en == "en", "Failed detection must fall back to script language 'en'"


def test_scenario_10_existing_english_campaign_backward_compatibility():
    script_text = "Welcome to our customer service hotline. How can we help you?"
    script_lang = detect_script_language(script_text)
    assert script_lang == "en"
    
    customer_speech = "I need help with my account bill."
    detected_lang = detect_speech_language(customer_speech, current_session_lang=script_lang, script_lang=script_lang)
    assert detected_lang == "en"
    
    prompt = format_multilingual_prompt("You are a customer service rep.", target_lang=detected_lang)
    assert "English" in prompt
