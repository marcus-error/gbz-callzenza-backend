import pytest
from app.services.script_parser import (
    parse_script_segments,
    validate_script_markup,
    strip_markup_tags,
    EmotionType,
)


def test_test_1_select_happy():
    script = '<emotion type="happy" />\nGood morning, sir.'
    segments = parse_script_segments(script)
    assert len(segments) == 1
    assert segments[0]["type"] == "speech"
    assert segments[0]["emotion"] == "happy"
    assert segments[0]["text"] == "Good morning, sir."


def test_test_2_happy_plus_pause():
    script = '<emotion type="happy" />\nGood morning, sir.\n<break time="1.0s" />\nHow are you today?'
    segments = parse_script_segments(script)
    assert len(segments) == 3
    # 1. First speech segment is happy
    assert segments[0]["type"] == "speech"
    assert segments[0]["emotion"] == "happy"
    assert segments[0]["text"] == "Good morning, sir."
    # 2. Pause of 1.0s
    assert segments[1]["type"] == "pause"
    assert segments[1]["duration"] == 1.0
    # 3. Subsequent speech segment continues with happy emotion
    assert segments[2]["type"] == "speech"
    assert segments[2]["emotion"] == "happy"
    assert segments[2]["text"] == "How are you today?"


def test_test_3_multiple_emotions():
    script = (
        '<emotion type="happy" />\nGood morning, sir.\n'
        '<emotion type="neutral" />\nI am calling from D-BUSINESS FORUM.\n'
        '<emotion type="surprised" />\nThat\'s wonderful!'
    )
    segments = parse_script_segments(script)
    assert len(segments) == 3
    assert segments[0]["emotion"] == "happy"
    assert segments[0]["text"] == "Good morning, sir."
    assert segments[1]["emotion"] == "neutral"
    assert segments[1]["text"] == "I am calling from D-BUSINESS FORUM."
    assert segments[2]["emotion"] == "surprised"
    assert segments[2]["text"] == "That's wonderful!"


def test_test_4_pause_plus_emotion_change():
    script = '<emotion type="happy" />\nGood morning.\n<break time="1.5s" />\n<emotion type="sad" />\nI understand your concern.'
    segments = parse_script_segments(script)
    assert len(segments) == 3
    assert segments[0]["emotion"] == "happy"
    assert segments[0]["text"] == "Good morning."
    assert segments[1]["type"] == "pause"
    assert segments[1]["duration"] == 1.5
    assert segments[2]["emotion"] == "sad"
    assert segments[2]["text"] == "I understand your concern."


def test_test_5_existing_script_without_tags():
    script = "Good morning Mr. [Name], I am [Agent Name]."
    segments = parse_script_segments(script, lead_name="John Doe", agent_name="Alex")
    assert len(segments) == 1
    assert segments[0]["type"] == "speech"
    assert segments[0]["emotion"] == "neutral"
    assert segments[0]["text"] == "Good morning Mr. John Doe, I am Alex."


def test_test_6_invalid_emotion_rejected():
    script = '<emotion type="angry" />\nWhy did this happen?'
    is_valid, err = validate_script_markup(script)
    assert is_valid is False
    assert "Unsupported emotion: 'angry'" in err

    with pytest.raises(ValueError) as exc:
        parse_script_segments(script, strict=True)
    assert "Unsupported emotion: 'angry'" in str(exc.value)


def test_test_7_invalid_pause_greater_than_10s_rejected():
    script = '<break time="15s" />\nToo long pause.'
    is_valid, err = validate_script_markup(script)
    assert is_valid is False
    assert "Invalid pause duration: 15.0s" in err

    with pytest.raises(ValueError) as exc:
        parse_script_segments(script, strict=True)
    assert "Invalid pause duration: 15.0s" in str(exc.value)


def test_invalid_pause_less_than_minimum_rejected():
    script = '<break time="0.05s" />\nToo short pause.'
    is_valid, err = validate_script_markup(script)
    assert is_valid is False
    assert "Invalid pause duration" in err


def test_strip_markup_tags():
    script = '<emotion type="happy" />\nHello world!\n<break time="0.5s" />\n<emotion type="neutral" />\nBye!'
    plain = strip_markup_tags(script)
    assert plain == "Hello world! Bye!"


@pytest.mark.asyncio
async def test_synthesize_script_audio_flow():
    from app.services.tts_service import synthesize_script_audio
    script = (
        '<emotion type="happy" />\nGood morning Mr. [Name].\n'
        '<break time="0.5s" />\n'
        'How are you today?\n'
        '<emotion type="neutral" />\n'
        'I am calling from D-BUSINESS FORUM.'
    )
    audio_bytes, url, segs = await synthesize_script_audio(script, lead_name="John Doe")
    assert len(audio_bytes) > 0
    assert url.startswith("http") or "/static/audio/" in url
    assert len(segs) == 4
    assert segs[0]["emotion"] == "happy"
    assert segs[1]["type"] == "pause"
    assert segs[1]["duration"] == 0.5
    assert segs[2]["emotion"] == "happy"
    assert segs[3]["emotion"] == "neutral"

