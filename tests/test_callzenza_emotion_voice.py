import pytest
from app.services.emotion_service import analyze_voice_emotion, CONFIDENCE_THRESHOLDS

def test_1_normal_customer_professional_tone():
    """Test 1: Normal customer receives professional_natural tone."""
    res = analyze_voice_emotion("Hello, I would like to inquire about your solar energy plans.")
    assert res["emotion"] == "neutral"
    assert res["tone"] == "professional_natural"
    assert res["speaking_rate"] == 1.00

def test_2_frustrated_customer_calm_empathetic_tone():
    """Test 2: Frustrated customer receives calm_empathetic tone with reduced speaking rate."""
    res = analyze_voice_emotion("I've already told your team this three times and no one helped me!")
    assert res["emotion"] in ("frustrated", "angry")
    assert res["tone"] == "calm_empathetic"
    assert res["speaking_rate"] == 0.90
    assert res["confidence"] >= CONFIDENCE_THRESHOLDS["MEDIUM"]
    assert "empathetic" in res["llm_instruction"].lower()

def test_3_angry_customer_escalation_trigger():
    """Test 3: Strongly angry customer across turns triggers escalation recommendation."""
    prev_state = {"emotion": "frustrated", "confidence": 0.85, "intensity": 0.80, "consecutive_frustrated_count": 1}
    res = analyze_voice_emotion("This is ridiculous! Transfer me to a human manager immediately!", previous_emotion_state=prev_state)
    assert res["emotion"] in ("frustrated", "angry")
    assert res["tone"] == "calm_empathetic"
    assert res["escalation_recommended"] is True

def test_4_happy_customer_warm_positive_tone():
    """Test 4: Happy customer receives warm_positive tone."""
    res = analyze_voice_emotion("That is fantastic news, thank you so much! I'm very happy with this offer.")
    assert res["emotion"] in ("happy", "positive")
    assert res["tone"] == "warm_positive"
    assert res["speaking_rate"] == 1.05
    assert "warm" in res["llm_instruction"].lower()

def test_5_confused_customer_patient_explanatory_tone():
    """Test 5: Confused customer receives patient_explanatory tone with step-by-step instruction."""
    res = analyze_voice_emotion("Wait, I don't understand what you mean, I'm really lost and confused.")
    assert res["emotion"] == "confused"
    assert res["tone"] == "patient_explanatory"
    assert res["speaking_rate"] == 0.85
    assert "step-by-step" in res["llm_instruction"].lower() or "simple" in res["llm_instruction"].lower()

def test_6_urgent_customer_direct_concise_tone():
    """Test 6: Urgent customer receives direct_concise tone."""
    res = analyze_voice_emotion("I need this ASAP right now! I have an emergency and no time.")
    assert res["emotion"] == "urgent"
    assert res["tone"] == "direct_concise"
    assert res["speaking_rate"] == 1.10
    assert "concise" in res["llm_instruction"].lower()

def test_7_emotion_transition_mid_call():
    """Test 7: Emotion transition across turns adapts agent tone smoothly."""
    # Turn 1: Frustrated
    res_t1 = analyze_voice_emotion("I'm annoyed because no one called me back.")
    assert res_t1["tone"] == "calm_empathetic"

    # Turn 2: Customer resolves and becomes positive
    res_t2 = analyze_voice_emotion("Oh wonderful, thank you for fixing that so quickly!", previous_emotion_state=res_t1)
    assert res_t2["emotion"] in ("happy", "positive")
    assert res_t2["tone"] == "warm_positive"

def test_8_barge_in_interruption_event():
    """Test 8: Customer interruption event during barge-in recalibrates emotion immediately."""
    audio_barge_in = {"is_interruption": True, "volume_intensity": 1.4, "speaking_rate": 1.3}
    res = analyze_voice_emotion("No, that's not what I asked at all!", audio_features=audio_barge_in)
    assert res["emotion"] in ("frustrated", "angry")
    assert res["confidence"] >= CONFIDENCE_THRESHOLDS["MEDIUM"]
    assert res["tone"] == "calm_empathetic"

def test_9_low_confidence_does_not_overreact():
    """Test 9: Low-confidence emotion (< 0.50) maintains normal professional tone without overreacting."""
    # Loud phone connection noise without frustrated words or interruption
    noisy_audio = {"volume_intensity": 1.35, "speaking_rate": 1.0}
    res = analyze_voice_emotion("Yes, I am calling about the appointment.", audio_features=noisy_audio)
    assert res["confidence"] < CONFIDENCE_THRESHOLDS["LOW"] or res["emotion"] == "neutral"
    assert res["tone"] == "professional_natural"

def test_10_multi_session_call_isolation():
    """Test 10: Emotion state remains 100% isolated between simultaneous calls."""
    call_a_state = analyze_voice_emotion("This is terrible, I am furious!")
    call_b_state = analyze_voice_emotion("Hello, good morning!")

    assert call_a_state["tone"] == "calm_empathetic"
    assert call_b_state["tone"] == "professional_natural"
    assert call_a_state["emotion"] != call_b_state["emotion"]
