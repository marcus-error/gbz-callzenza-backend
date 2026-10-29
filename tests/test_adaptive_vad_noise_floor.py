import pytest
import math


class AdaptiveVADSimulator:
    def __init__(self, initial_noise_floor: float = 16.0):
        self.noise_floor = initial_noise_floor
        self.is_speaking = False
        self.is_bot_speaking = False
        self.barge_trigger_count = 0

    def get_threshold(self) -> float:
        if self.is_bot_speaking:
            return 300.0
        return max(35.0, min(150.0, self.noise_floor + 20.0))

    def process_frame(self, rms: float) -> bool:
        current_threshold = self.get_threshold()

        # Update noise floor ONLY during silence/ambient frames when bot is not speaking
        if not self.is_bot_speaking and not self.is_speaking and rms < current_threshold:
            self.noise_floor = max(10.0, min(80.0, self.noise_floor * 0.95 + rms * 0.05))

        if self.is_bot_speaking:
            if rms > 300:
                self.barge_trigger_count += 1
                if self.barge_trigger_count >= 2:
                    self.is_bot_speaking = False
                    self.barge_trigger_count = 0
            else:
                self.barge_trigger_count = max(0, self.barge_trigger_count - 1)

        if not self.is_speaking:
            if rms > current_threshold and not self.is_bot_speaking:
                self.is_speaking = True
        else:
            if rms <= current_threshold:
                self.is_speaking = False

        return self.is_speaking


def test_1_and_2_noise_rms_16_and_20_remain_non_speech():
    vad = AdaptiveVADSimulator(initial_noise_floor=16.0)
    assert vad.get_threshold() == 36.0  # max(35.0, 16.0 + 20.0)

    # Frame 1: Noise RMS 16
    assert vad.process_frame(16.0) is False

    # Frame 2: Noise RMS 20
    assert vad.process_frame(20.0) is False
    assert vad.is_speaking is False


def test_3_soft_speech_rms_45_detected():
    vad = AdaptiveVADSimulator(initial_noise_floor=16.0)
    # Threshold is 36.0, soft speech at 45.0 should trigger speech detection
    assert vad.process_frame(45.0) is True


def test_4_and_5_speech_rms_70_and_95_detected():
    vad = AdaptiveVADSimulator(initial_noise_floor=16.0)
    assert vad.process_frame(70.0) is True

    vad2 = AdaptiveVADSimulator(initial_noise_floor=16.0)
    assert vad2.process_frame(95.0) is True


def test_6_loud_speech_500_plus_detected():
    vad = AdaptiveVADSimulator(initial_noise_floor=16.0)
    assert vad.process_frame(500.0) is True
    assert vad.process_frame(2500.0) is True


def test_7_noise_spikes_do_not_permanently_raise_noise_floor():
    vad = AdaptiveVADSimulator(initial_noise_floor=16.0)
    initial_floor = vad.noise_floor

    # Process a brief non-speech noise spike of 30.0 for 2 frames
    vad.process_frame(30.0)
    vad.process_frame(30.0)

    # Floor slightly adjusted from 16.0 -> 17.33
    spike_floor = vad.noise_floor
    assert spike_floor < 20.0

    # Process 10 silence frames at 16.0
    for _ in range(10):
        vad.process_frame(16.0)

    # Floor returns back near initial baseline
    assert vad.noise_floor < 17.0


def test_8_noise_floor_isolated_between_simultaneous_calls():
    vad_call_1 = AdaptiveVADSimulator(initial_noise_floor=16.0)
    vad_call_2 = AdaptiveVADSimulator(initial_noise_floor=40.0)  # Noisier line

    # Call 1 receives quiet frames
    vad_call_1.process_frame(16.0)
    # Call 2 receives frames at 40.0
    vad_call_2.process_frame(40.0)

    assert vad_call_1.noise_floor < 20.0
    assert vad_call_1.get_threshold() < 40.0

    assert vad_call_2.noise_floor > 35.0
    assert vad_call_2.get_threshold() >= 55.0


def test_9_bot_speaking_uses_300_threshold():
    vad = AdaptiveVADSimulator(initial_noise_floor=16.0)
    vad.is_bot_speaking = True
    assert vad.get_threshold() == 300.0

    # Speech below 300.0 does not trigger barge-in/speech
    assert vad.process_frame(200.0) is False

    # High speech above 300.0 for 2 frames triggers barge-in and interrupts bot
    vad.process_frame(350.0)
    assert vad.process_frame(350.0) is True
    assert vad.is_bot_speaking is False


def test_10_vad_stt_transition_lifecycle():
    vad = AdaptiveVADSimulator(initial_noise_floor=16.0)
    
    # 1. Silence -> non-speech
    assert vad.process_frame(16.0) is False
    
    # 2. Customer speaks RMS 80 -> triggers customer_audio_started
    assert vad.process_frame(80.0) is True
    
    # 3. Customer continues speaking -> stays speaking
    assert vad.process_frame(120.0) is True
    
    # 4. Customer stops speaking (silence RMS 16) -> ends speech
    assert vad.process_frame(16.0) is False
