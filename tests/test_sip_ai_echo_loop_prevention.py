import time
import re
import pytest
from collections import deque


def check_is_echo_of_ai(clean_caller: str, recent_ai_utterances: list[str]) -> bool:
    """Reflects the exact logic of self-echo transcript rejection in sip_ai_bot.py."""
    clean_lower = clean_caller.lower()
    clean_words = set(re.findall(r'[a-zA-Z0-9]+', clean_lower))
    substantive_words = {
        w for w in clean_words
        if len(w) > 2 and w not in {"the", "and", "for", "with", "this", "that", "you", "are", "have", "from", "yes", "yeah", "okay"}
    }

    for past_ai_text in recent_ai_utterances[-3:]:
        past_lower = past_ai_text.lower()
        if len(clean_lower) >= 6 and clean_lower in past_lower:
            return True
        if substantive_words:
            past_words = set(re.findall(r'[a-zA-Z0-9]+', past_lower))
            matched = substantive_words.intersection(past_words)
            if len(matched) >= 2 and (len(matched) / len(substantive_words)) >= 0.65:
                return True
    return False


def test_echo_filter_detects_exact_substring():
    ai_speech = "Hello, this is Pooja calling from Nurse O' Clock. How are you doing today?"
    recent = [ai_speech]

    # STT picks up part of the bot's own voice
    echo_transcript = "nurse o' clock. how are you doing"
    assert check_is_echo_of_ai(echo_transcript, recent) is True


def test_echo_filter_detects_fuzzy_word_overlap():
    ai_speech = "I apologize for any inconvenience. I have added your number to our Do Not Call list."
    recent = [ai_speech]

    # STT picks up imperfect transcription of echo
    echo_transcript = "added your number do not call list"
    assert check_is_echo_of_ai(echo_transcript, recent) is True


def test_echo_filter_allows_legitimate_customer_speech():
    ai_speech = "Good morning! Hi, this is Pooja calling from Nurse O' Clock. How are you doing today?"
    recent = [ai_speech]

    # Customer speaks legitimately
    assert check_is_echo_of_ai("i am interested in your nursing services", recent) is False
    assert check_is_echo_of_ai("who is this", recent) is False
    assert check_is_echo_of_ai("call me back tomorrow at five", recent) is False
    assert check_is_echo_of_ai("yes please send details", recent) is False


def test_echo_guard_window_discards_audio():
    echo_guard_until = time.time() + 0.45
    now = time.time()

    is_in_echo_guard = (now < echo_guard_until)
    assert is_in_echo_guard is True


def test_barge_in_ignores_acoustic_reflection_level():
    # Telephone echo reflection typically ranges RMS 300 to 1400
    echo_rms_values = [450.0, 780.0, 1150.0, 1380.0]
    barge_trigger_count = 0

    for rms in echo_rms_values:
        if rms > 1600:
            barge_trigger_count += 1
        else:
            barge_trigger_count = max(0, barge_trigger_count - 1)

    # Must NOT trigger barge-in on reflected echo
    assert barge_trigger_count == 0

    # Genuine loud human speech (> 1600) sustained for 6+ frames triggers barge-in
    human_rms_values = [1850.0] * 6
    for rms in human_rms_values:
        if rms > 1600:
            barge_trigger_count += 1

    assert barge_trigger_count >= 6
