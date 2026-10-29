"""
Wrapper exposing HumanHandoffIntentDetector under app.services.
"""
from ai.agents.intent.human_handoff_intent import (
    HumanHandoffIntentDetector,
    IntentDetectionResult,
)

__all__ = ["HumanHandoffIntentDetector", "IntentDetectionResult"]
