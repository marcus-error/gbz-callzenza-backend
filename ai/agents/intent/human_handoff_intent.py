"""
Multi-lingual Intent Detection for AI Agent -> Human Voice Agent Handoff.
Supports English, Tamil, Tanglish, and Hindi with high-precision keyword, regex,
and false-positive guards.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, List, Tuple


@dataclass
class IntentDetectionResult:
    has_intent: bool
    confidence: float
    language: str
    matched_phrase: Optional[str] = None
    requires_confirmation: bool = False
    reason: str = ""


# Negative guard patterns that mention "agent" or "bot" but are NOT transfer requests
NEGATIVE_IDENTITY_PATTERNS = [
    r'\b(?:are\s+you|ur|you\'re|is\s+this|are\s+u)\s+(?:an?\s+)?(?:ai|bot|virtual|automated|robot|computer)\s*(?:agent)?\b',
    r'\b(?:ai\s+agent|bot\s+agent|virtual\s+agent|automated\s+agent)\b',
    r'\bwho\s+is\s+(?:this|the)\s+agent\b',
    r'\bwhat\s+(?:is\s+your|kind\s+of)\s+agent\b',
]

NEGATIVE_TOPIC_PATTERNS = [
    r'\b(?:agent\s+commission|agent\s+fee|lic\s+agent|insurance\s+agent|real\s+estate\s+agent)\b',
    r'\b(?:lump\s+sum|policy\s+benefit|maturity\s+benefit)\b',
]

# High-confidence exact phrases by language
PHRASES_ENGLISH: List[str] = [
    "i want to talk with agent",
    "i want to talk to an agent",
    "i want to talk to agent",
    "talk to an agent",
    "talk to agent",
    "talk to human agent",
    "talk to voice agent",
    "talk to a human",
    "talk to human",
    "talk to real person",
    "talk to a person",
    "talk to someone else",
    "talk to someone",
    "talk with someone",
    "speak to someone",
    "speak with someone",
    "can i talk to someone",
    "can i speak to someone",
    "can i speak with someone",
    "talk with agent",
    "talk with human",
    "speak to an agent",
    "speak to agent",
    "speak to human agent",
    "speak to voice agent",
    "speak to a human",
    "speak to human",
    "speak to a person",
    "speak with a person",
    "speak with an agent",
    "speak with customer support",
    "speak to customer support",
    "talk to customer support",
    "talk with customer support",
    "i need to speak with customer support",
    "i need to talk to customer support",
    "i want to speak with customer support",
    "i want to talk to customer support",
    "connect me to customer support",
    "transfer me to customer support",
    "i need customer support",
    "i need customer care",
    "customer support representative",
    "customer care representative",
    "support agent",
    "support representative",
    "support team",
    "connect me to a person",
    "connect me to an agent",
    "connect me to a human",
    "can you connect me to a human",
    "can you connect me to an agent",
    "connect to agent",
    "connect to human",
    "connect me to human",
    "transfer to voice agent",
    "transfer to human agent",
    "transfer to agent",
    "transfer to human",
    "transfer to a person",
    "transfer my call",
    "transfer call",
    "transfer the call",
    "transfer me to human",
    "transfer me to an agent",
    "transfer me to your representative",
    "please transfer me to your representative",
    "please transfer me to an agent",
    "please transfer me to a representative",
    "give me a human",
    "put me through to a human",
    "put me through to an agent",
    "live human agent",
    "live agent",
    "human agent",
    "human representative",
    "customer service representative",
    "switch to human",
    "switch to agent",
    "i would like to speak to a real person",
    "i would like to talk to a real person",
    "i want to speak to a real person",
    "i want to talk to a real person",
    "can i speak with a real person",
    "can i talk to a real person",
]

PHRASES_TAMIL: List[str] = [
    "agent kitta pesanum",
    "agent kita pesanum",
    "agent oda pesanum",
    "agent kooda pesanum",
    "agent kita connect panu",
    "agent kitta connect pannunga",
    "agent ku transfer pannunga",
    "agent ku call transfer pannunga",
    "agent kitta pesa mudiyuma",
    "manushan kitta pesanum",
    "manusan kita pesanum",
    "human kitta pesanum",
    "real person kitta pesanum",
    "oru agent kitta pesanum",
    "oru human kitta connect pannunga",
    "agent peasanum",
    "agent kitta matum pesanum",
    "call transfer pannunga",
    "customer care kitta pesanum",
    "support kitta pesanum",
    "நேரடி முகவருடன் பேச வேண்டும்",
    "மனிதரிடம் பேச வேண்டும்",
    "முகவருடன் பேச வேண்டும்",
    "ஏஜென்ட் உடன் பேச வேண்டும்",
    "ஏஜெண்டிடம் பேசுங்கள்",
]

PHRASES_HINDI: List[str] = [
    "mujhe agent se baat karni hai",
    "agent se baat karni hai",
    "agent se baat karao",
    "agent se connect karo",
    "kisi insaan se baat karni hai",
    "kisi insaan se baat karao",
    "insaan se baat karwao",
    "human agent se baat karni hai",
    "human agent chahiye",
    "call transfer karo",
    "agent ko call transfer karo",
    "kisi representative se baat karao",
    "customer care agent se baat karni hai",
    "customer care se baat karni hai",
    "customer care se baat karao",
    "customer support se baat karni hai",
    "customer support se baat karao",
    "support team se baat karao",
    "mujhe kisi se baat karni hai",
    "mujhe human se baat karni hai",
    "मुझे एजेंट से बात करनी है",
    "एजेंट से बात कराओ",
    "इंसान से बात कराओ",
    "कॉल ट्रांसफर करो",
    "एजेंट चाहिए",
]


class HumanHandoffIntentDetector:
    """Classifies user speech for handoff requests across English, Tamil, Tanglish, and Hindi."""

    @classmethod
    def detect(cls, text: str) -> IntentDetectionResult:
        if not text or not text.strip():
            return IntentDetectionResult(
                has_intent=False,
                confidence=0.0,
                language="unknown",
                reason="Empty input text"
            )

        cleaned = text.lower().strip()

        # Step 1: Check negative guards first
        for pat in NEGATIVE_IDENTITY_PATTERNS:
            if re.search(pat, cleaned):
                return IntentDetectionResult(
                    has_intent=False,
                    confidence=0.1,
                    language="en",
                    reason="Matched identity inquiry (asking if bot/AI)"
                )

        for pat in NEGATIVE_TOPIC_PATTERNS:
            if re.search(pat, cleaned):
                return IntentDetectionResult(
                    has_intent=False,
                    confidence=0.1,
                    language="en",
                    reason="Matched policy/topic keyword, not transfer request"
                )

        # Step 2: Tamil / Tanglish high-confidence match
        for phrase in PHRASES_TAMIL:
            if phrase in cleaned:
                return IntentDetectionResult(
                    has_intent=True,
                    confidence=0.95,
                    language="ta",
                    matched_phrase=phrase,
                    requires_confirmation=False,
                    reason=f"Matched Tamil handoff phrase: '{phrase}'"
                )

        # Step 3: Hindi match
        for phrase in PHRASES_HINDI:
            if phrase in cleaned:
                return IntentDetectionResult(
                    has_intent=True,
                    confidence=0.95,
                    language="hi",
                    matched_phrase=phrase,
                    requires_confirmation=False,
                    reason=f"Matched Hindi handoff phrase: '{phrase}'"
                )

        # Step 4: English high-confidence match
        for phrase in PHRASES_ENGLISH:
            if phrase in cleaned:
                return IntentDetectionResult(
                    has_intent=True,
                    confidence=0.95,
                    language="en",
                    matched_phrase=phrase,
                    requires_confirmation=False,
                    reason=f"Matched English handoff phrase: '{phrase}'"
                )

        # Step 5: Flexible Regex Pattern Matching (e.g. "could you please transfer this to a live agent")
        verb_target_regex = (
            r'\b(talk|speak|transfer|connect|switch|pass|get|put|call|reach|chat)\b'
            r'.{0,35}'
            r'\b(human|person|representative|agent|rep|expert|specialist|someone|somebody|live agent|real person|customer support|customer care|support agent|support team|support rep|support)\b'
        )
        match = re.search(verb_target_regex, cleaned)
        if match:
            return IntentDetectionResult(
                has_intent=True,
                confidence=0.92,
                language="en",
                matched_phrase=match.group(0),
                requires_confirmation=False,
                reason=f"Matched action-target pattern: '{match.group(0)}'"
            )

        # Step 6: Tanglish regex (e.g. "...agent kitta...")
        tanglish_regex = r'\b(agent|human|manushan|customer care|support)\b.{0,25}\b(pesanum|connect|transfer|pesa|kooda|pannunga|panu)\b'
        match_ta = re.search(tanglish_regex, cleaned)
        if match_ta:
            return IntentDetectionResult(
                has_intent=True,
                confidence=0.92,
                language="ta",
                matched_phrase=match_ta.group(0),
                requires_confirmation=False,
                reason=f"Matched Tanglish pattern: '{match_ta.group(0)}'"
            )

        # Step 7: Hindi regex (e.g. "...agent se...")
        hindi_regex = r'\b(agent|insaan|human|representative|customer care|support)\b.{0,25}\b(baat|connect|transfer|karao|karwao|chahiye|karni)\b'
        match_hi = re.search(hindi_regex, cleaned)
        if match_hi:
            return IntentDetectionResult(
                has_intent=True,
                confidence=0.92,
                language="hi",
                matched_phrase=match_hi.group(0),
                requires_confirmation=False,
                reason=f"Matched Hindi regex pattern: '{match_hi.group(0)}'"
            )

        # Step 8: Medium-confidence confirmation trigger (e.g. isolated "agent" word in question)
        if re.search(r'\b(?:human|agent|representative)\b', cleaned):
            if any(w in cleaned for w in ("can i", "is there", "possible", "please", "available")):
                return IntentDetectionResult(
                    has_intent=True,
                    confidence=0.65,
                    language="en",
                    matched_phrase="agent query",
                    requires_confirmation=True,
                    reason="Ambiguous agent inquiry requiring customer confirmation"
                )

        return IntentDetectionResult(
            has_intent=False,
            confidence=0.0,
            language="en",
            reason="No handoff intent detected"
        )
