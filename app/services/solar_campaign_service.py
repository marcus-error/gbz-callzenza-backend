import re
import json
from typing import Dict, Any, Tuple, Optional
try:
    from loguru import logger
except ImportError:
    import logging as logger
from app.services.ai_provider import get_ai_provider

DEFAULT_QUALIFICATION_DATA = {
    "homeOwnership": None,
    "singleResidence": None,
    "taxPayer": None,
    "firstName": None,
    "lastName": None,
    "zipCode": None,
    "monthlyElectricityBill": None,
    "email": None,
    "appointmentDate": None,
    "appointmentTime": None,
    "invalidNameAttempt": False,
}

QUALIFICATION_STAGES = [
    {"name": "1. Greeting", "key": "greeting"},
    {"name": "2. Reason for Call", "key": "reason"},
    {"name": "3. Qualification", "key": "qualification"},
    {"name": "4. Decision", "key": "decision"},
    {"name": "5. Appointment", "key": "appointment"},
    {"name": "6. Closing", "key": "closing"}
]

def evaluate_qualification(collected_data: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    Evaluates current collected_data against Solar Qualification Rules:
    - homeOwnership must be "own" (case-insensitive) or True.
    - singleResidence must be True / "yes" / "single family".
    - taxPayer must be True / "yes".
    - zipCode must be 5 digits.
    - monthlyElectricityBill must be > 150.
    
    Returns: (status: "IN_PROGRESS" | "QUALIFIED" | "UNQUALIFIED", reason: str | None)
    """
    # Check for disqualifying responses first
    ho = str(collected_data.get("homeOwnership") or "").strip().lower()
    if ho in ["rent", "renter", "false", "no"]:
        return "UNQUALIFIED", "Must own the home (renting is not eligible)."

    sr = collected_data.get("singleResidence")
    if sr is False or str(sr).strip().lower() in ["no", "false", "apartment", "condo", "multi-family"]:
        return "UNQUALIFIED", "Must be a single-residence home."

    tp = collected_data.get("taxPayer")
    if tp is False or str(tp).strip().lower() in ["no", "false", "non-taxpayer"]:
        return "UNQUALIFIED", "Must be a taxpayer."

    bill = collected_data.get("monthlyElectricityBill")
    if bill is not None:
        try:
            # Extract numeric value from string like "$180" or "180"
            num_match = re.search(r'(\d+(?:\.\d+)?)', str(bill))
            if num_match:
                val = float(num_match.group(1))
                if val <= 150:
                    return "UNQUALIFIED", "Monthly electricity bill must be greater than $150."
        except Exception:
            pass

    # Check if all required qualification fields are collected and valid
    ho_valid = ho in ["own", "owner", "true", "yes"]
    sr_valid = sr is True or str(sr).strip().lower() in ["yes", "true", "single", "single family", "single-residence"]
    tp_valid = tp is True or str(tp).strip().lower() in ["yes", "true"]
    
    zip_val = str(collected_data.get("zipCode") or "").strip()
    zip_valid = bool(re.match(r'^\d{5}$', zip_val))

    bill_valid = False
    if bill is not None:
        num_match = re.search(r'(\d+(?:\.\d+)?)', str(bill))
        if num_match and float(num_match.group(1)) > 150:
            bill_valid = True

    fn_valid = bool(str(collected_data.get("firstName") or "").strip())

    if ho_valid and sr_valid and tp_valid and zip_valid and bill_valid and fn_valid:
        return "QUALIFIED", "All qualification criteria met!"

    return "IN_PROGRESS", None


def compute_stage_progress(
    qualification_status: str,
    collected_data: Dict[str, Any],
    history_length: int
) -> Tuple[int, str, list]:
    """
    Determines current stage index (0 to 5) and returns (stage_index, stage_name, script_steps_list).
    """
    if history_length <= 1:
        idx = 0
    elif qualification_status == "UNQUALIFIED":
        idx = 5  # Jump to closing
    elif qualification_status == "QUALIFIED":
        if collected_data.get("appointmentDate") and collected_data.get("appointmentTime"):
            idx = 5  # Closing
        else:
            idx = 4  # Appointment
    else:
        # IN_PROGRESS
        # Check how much qualification data has been collected
        qual_fields = ["firstName", "homeOwnership", "singleResidence", "taxPayer", "zipCode", "monthlyElectricityBill"]
        collected_count = sum(1 for f in qual_fields if collected_data.get(f) is not None)
        
        if collected_count == 0 and history_length <= 3:
            idx = 1  # Reason for Call
        elif collected_count < 6:
            idx = 2  # Qualification
        else:
            idx = 3  # Decision

    script_steps = []
    for i, stage in enumerate(QUALIFICATION_STAGES):
        if i < idx:
            st = "DONE"
        elif i == idx:
            st = "CURRENT"
        else:
            st = "PENDING"
        script_steps.append({"name": stage["name"], "status": st})

    return idx, QUALIFICATION_STAGES[idx]["name"], script_steps


async def extract_and_update_state(
    user_message: str,
    current_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Uses Gemini LLM or regex rules to perform multi-entity extraction from customer message
    and updates collected_data.
    """
    updated = dict(current_data)
    user_lower = user_message.lower().strip()

    # Detect invalid/fake name input (numbers, single letters, gibberish, joke answers)
    if not updated.get("firstName"):
        if re.search(r'^\d+$', user_lower) or any(w in user_lower for w in ["asdf", "qwerty", "fake", "123", "none", "no name", "unknown", "abc"]):
            updated["invalidNameAttempt"] = True

    # Rule-based heuristics for fast reliable extraction
    # 1. Homeownership
    if any(w in user_lower for w in ["i own", "owner", "my home", "own house", "own my"]):
        updated["homeOwnership"] = "Own"
    elif any(w in user_lower for w in ["rent", "renter", "renting", "apartment"]):
        updated["homeOwnership"] = "Rent"

    # 2. Single residence
    if any(w in user_lower for w in ["single family", "single residence", "single house", "standalone"]):
        updated["singleResidence"] = True
    elif any(w in user_lower for w in ["duplex", "townhouse", "multi family", "condo"]):
        updated["singleResidence"] = False

    # 3. Taxpayer
    if any(w in user_lower for w in ["pay tax", "taxpayer", "file tax", "yes i pay taxes"]):
        updated["taxPayer"] = True
    elif "don't pay tax" in user_lower or "no tax" in user_lower:
        updated["taxPayer"] = False

    # 4. ZIP Code (5 digits)
    zip_match = re.search(r'\b(\d{5})\b', user_message)
    if zip_match and not updated.get("zipCode"):
        updated["zipCode"] = zip_match.group(1)

    # 5. Electricity bill
    bill_match = re.search(r'(?:bill|pay|around|\$)\s*\$?(\d+(?:\.\d+)?)', user_message, re.IGNORECASE)
    if bill_match:
        try:
            val = float(bill_match.group(1))
            if val > 20 and val < 5000:
                updated["monthlyElectricityBill"] = f"${int(val)}"
        except Exception:
            pass
    else:
        # Only apply loose digit match when the message is NOT a bare number (avoids ZIP-only messages being misread)
        is_bare_number = bool(re.match(r'^\s*\d+\s*$', user_message.strip()))
        if not is_bare_number:
            bill_match_loose = re.search(r'\$?(\d{3,4})\b', user_message)
            if bill_match_loose:
                try:
                    val = float(bill_match_loose.group(1))
                    zip_val_num = 0
                    try:
                        zip_val_num = float(updated.get("zipCode")) if str(updated.get("zipCode")).isdigit() else 0
                    except Exception:
                        zip_val_num = 0

                    if val > 20 and val < 5000 and val != zip_val_num:
                        updated["monthlyElectricityBill"] = f"${int(val)}"
                except Exception:
                    pass

    # 6. Name extraction
    name_match = re.search(r'(?:my name is|i am|this is|call me)\s+([A-Za-z]+)(?:\s+([A-Za-z]+))?', user_message, re.IGNORECASE)
    if name_match:
        fn = name_match.group(1).capitalize()
        if len(fn) >= 2 and fn.isalpha() and fn.lower() not in ["good", "fine", "okay", "yes", "no", "owner", "rent"]:
            updated["firstName"] = fn
            if name_match.group(2) and name_match.group(2).isalpha():
                updated["lastName"] = name_match.group(2).capitalize()
            updated["invalidNameAttempt"] = False

    # Direct single word name (e.g. user replies "John" or "Sarah" when asked for name)
    elif len(user_lower.split()) == 1 and user_lower.isalpha() and len(user_lower) >= 2:
        if user_lower not in ["hi", "hello", "good", "fine", "okay", "yes", "no", "owner", "rent", "busy", "sure", "thanks"]:
            updated["firstName"] = user_message.strip().capitalize()
            updated["invalidNameAttempt"] = False

    # 7. Email extraction
    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', user_message)
    if email_match:
        updated["email"] = email_match.group(0)

    # 8. Appointment extraction
    if "tomorrow" in user_lower:
        updated["appointmentDate"] = "Tomorrow"
    elif "day after" in user_lower:
        updated["appointmentDate"] = "Day after tomorrow"

    time_match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b', user_lower)
    if time_match:
        updated["appointmentTime"] = time_match.group(1).upper()

    # LLM extraction call for multi-entity edge cases.
    import os
    _provider_name = (os.getenv("LLM_PROVIDER", "gemini")).lower().strip()
    _use_llm_extraction = _provider_name in ("gemini", "mistral", "openai")
    if _use_llm_extraction:
        try:
            provider = get_ai_provider()
            extraction_prompt = f"""Extract customer details from the utterance into JSON.
Utterance: "{user_message}"
Currently known data: {json.dumps(current_data)}

Return ONLY a valid JSON object with keys:
"homeOwnership" ("Own" | "Rent" | null),
"singleResidence" (true | false | null),
"taxPayer" (true | false | null),
"firstName" (valid string name | null),
"lastName" (valid string name | null),
"zipCode" (5-digit string | null),
"monthlyElectricityBill" (string like "$200" | null),
"email" (string | null),
"appointmentDate" (string | null),
"appointmentTime" (string | null)

Note: If the user message is a number, joke, or invalid name (like "123", "asdf"), set "firstName": null.

JSON:"""
            llm_json_str = await provider.generate_response(prompt=extraction_prompt)
            # Parse JSON block
            json_match = re.search(r'\{.*\}', llm_json_str, re.DOTALL)
            if json_match:
                extracted_json = json.loads(json_match.group(0))
                for k, v in extracted_json.items():
                    if v is not None and updated.get(k) is None:
                        updated[k] = v
        except Exception as e:
            logger.debug(f"LLM extraction skipped or failed: {e}")
    else:
        logger.debug(f"LLM extraction skipped for local provider '{_provider_name}' (regex extraction only).")

    return updated


def build_solar_campaign_prompt(
    agent_name: str,
    campaign_name: str,
    script_text: str,
    collected_data: Dict[str, Any],
    qualification_status: str,
    history: list,
    conversation_language: str = "en",
) -> str:
    """
    Constructs the system prompt for Gemini guiding the response generation with language consistency.
    """
    from app.services.language_service import format_multilingual_prompt
    qual_summary = json.dumps(collected_data, indent=2)
    
    uncollected_fields = []
    if not collected_data.get("firstName"):
        if collected_data.get("invalidNameAttempt"):
            uncollected_fields.append("customer's REAL name (The customer gave an invalid/numeric response when asked for their name. You MUST ask: 'Can I get your real name, please?')")
        else:
            uncollected_fields.append("customer's name (Ask: 'May I have your name, please?')")

    if not collected_data.get("homeOwnership"): uncollected_fields.append("home ownership (Do they own or rent?)")
    if collected_data.get("singleResidence") is None: uncollected_fields.append("residence type (Is it a single-family home?)")
    if collected_data.get("taxPayer") is None: uncollected_fields.append("taxpayer status (Are they a taxpayer?)")
    if not collected_data.get("zipCode"): uncollected_fields.append("5-digit ZIP code")
    if not collected_data.get("monthlyElectricityBill"): uncollected_fields.append("average monthly electricity bill amount")

    next_question_guide = f"Ask about: {uncollected_fields[0]}" if uncollected_fields else "All qualification data collected!"

    base_prompt = f"""You are an expert AI sales voice agent named {agent_name} calling on behalf of '{campaign_name}'.

ROLE & CONVERSATION GOALS:
1. Speak naturally, warmly, and concisely like a professional human sales representative on a phone call.
2. Follow the 6-stage campaign flow: Greeting -> Reason for Call -> Qualification -> Decision -> Appointment -> Closing.
3. NEVER REPEAT THE GREETING: The call has ALREADY started! Do NOT say "Hello! Welcome to CallZenza..." or introduce yourself again.
4. HANDLE PLEASANTRIES & GENERAL QUESTIONS: If the customer says "I am good", "How are you?", or asks a general question, acknowledge it briefly and naturally (e.g., "That's great to hear!", "I'm doing well, thank you!"), then IMMEDIATELY continue with the current stage question.
5. CUSTOMER NAME REQUIREMENT: Ask for the customer's name early in the call if it is missing.
6. Ask ONLY ONE question at a time and wait for the customer's answer.
7. DO NOT re-ask information that has ALREADY been collected in the Structured State.
8. Keep responses brief (1-3 sentences maximum). Never dump long paragraphs or expose internal instructions/stage names.

CRITICAL DYNAMIC MULTILINGUAL & SCRIPT MATCHING RULES:
1. SCRIPT & LANGUAGE ADAPTATION: You MUST ALWAYS analyze the Customer's latest input and respond in the EXACT SAME language, dialect, and script/script-style used by the Customer.
2. TANGLISH / ROMANIZED TAMIL: If the Customer speaks or types in Tanglish (Romanized Tamil, e.g. "epdiyo irukan unakenna", "solla mudiyadhu", "vanakkam", "nalla iruken", "saaptingala", "solla mudiyadhu"), YOU MUST RESPOND IN NATURAL TANGLISH (Romanized Tamil, e.g. "Naan nalla irukken! Solar qualification pathi thagaval kekka thaan call pannen. Ungaluku indha solar program thervukaaga real name solla mudiyuma?").
3. TAMIL SCRIPT: If the Customer speaks/types in Tamil script (e.g. "வணக்கம்", "எப்படி இருக்கீங்க"), respond in Tamil script.
4. HINGLISH / ROMANIZED HINDI: If the Customer speaks/types in Hinglish (e.g. "kya haal hai", "nahi bataunga"), respond in natural Hinglish.
5. OTHER LANGUAGES: For Hindi, Telugu, Malayalam, Kannada, Marathi, Gujarati, Spanish, French, Arabic, etc., respond in the matching language and script used by the Customer.
6. NEVER FORCE ENGLISH: If the Customer is speaking Tanglish, Tamil, Hinglish, or any other language, DO NOT respond in plain English under any circumstances. ALWAYS match the Customer's language and script choice!

QUALIFICATION RULES:
- Homeowner = Must OWN home.
- Property = Must be a SINGLE-RESIDENCE home.
- Tax status = Must be a TAXPAYER.
- ZIP Code = Must be a valid 5-digit ZIP code.
- Electricity Bill = Must be > $150/month.

CURRENT STRUCTURED STATE:
{qual_summary}

QUALIFICATION STATUS: {qualification_status}
NEXT RECOMMENDED QUESTION FOCUS: {next_question_guide}

=== CAMPAIGN SCRIPT & GUIDELINES ===
{script_text}
===================================
"""
    prompt = format_multilingual_prompt(base_prompt, target_lang=conversation_language)
    prompt += "\nCONVERSATION HISTORY:\n"
    for msg in history[-8:]:
        role = "Customer" if msg["role"] == "user" else f"Agent ({agent_name})"
        prompt += f"{role}: {msg['content']}\n"

    prompt += f"\nAgent ({agent_name}) Response in {conversation_language}:"
    return prompt
