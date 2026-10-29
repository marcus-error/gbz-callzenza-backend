"""
Universal Script Intelligence Engine — v3.
Dynamic normalization, semantic branch resolution, YES/NO branch action extraction,
objection matching, and post-objection rules.
Works for ANY uploaded agent script (Solar, Hospital, Insurance, Real Estate, Banking, SaaS, etc.).
Zero hardcoded business or industry logic.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from loguru import logger


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ScriptStep:
    step_index: int
    step_id: str
    title: str
    question_to_ask: Optional[str] = None
    required_field: Optional[str] = None
    on_positive_step: Optional[int] = None
    on_negative_step: Optional[int] = None
    raw_instructions: str = ""
    # Objections specific to this step
    objections: List[Dict[str, str]] = field(default_factory=list)
    # Positive / negative keywords hinted in the step text
    positive_keywords: List[str] = field(default_factory=list)
    negative_keywords: List[str] = field(default_factory=list)
    # Branch actions extracted from script (e.g. "ask_callback_time", "advance_script")
    positive_branch_action: Optional[str] = None
    positive_branch_text: Optional[str] = None   # raw instruction text from script
    negative_branch_action: Optional[str] = None
    negative_branch_text: Optional[str] = None   # raw instruction text from script


@dataclass
class NormalizedScript:
    agent_name: str = "AI Representative"
    business_name: str = "our organization"
    outbound_purpose: str = "sharing information and assisting you with our services"
    greeting: str = "Hello! How can I assist you today?"
    objective: str = ""
    call_direction: str = "outbound"
    steps: List[ScriptStep] = field(default_factory=list)
    closing_message: str = "Thank you for your time! Have a wonderful day."
    # Global objection section — applies across all steps
    global_objections: List[Dict[str, str]] = field(default_factory=list)
    # Post-objection rule (e.g. "After answering objection, always ask X")
    post_objection_rule: Optional[str] = None
    # Raw text for Gemini grounding
    raw_text: str = ""

    def get_step_by_index(self, index: int) -> Optional[ScriptStep]:
        if 0 <= index < len(self.steps):
            return self.steps[index]
        return None

    def get_current_step_question(self, index: int) -> Optional[str]:
        """Returns the question to ask at the given step index."""
        step = self.get_step_by_index(index)
        return step.question_to_ask if step else None


# ---------------------------------------------------------------------------
# Script Normalizer
# ---------------------------------------------------------------------------

class ScriptNormalizer:
    """
    Parses and normalizes ANY uploaded agent script into a universal runtime representation.
    Handles free-form text, markdown, structured scripts, and mixed documents.
    """

    @classmethod
    def parse_script(
        cls,
        script_text: str,
        agent_name: Optional[str] = None,
        business_name: Optional[str] = None,
    ) -> NormalizedScript:
        raw_text = script_text.strip() if script_text else ""
        resolved_agent = agent_name or "AI Representative"
        resolved_business = business_name or "our organization"

        if not raw_text:
            return cls._default_script(resolved_agent, resolved_business)

        # ── 1. Extract Agent Name ───────────────────────────────────────────
        name_match = re.search(
            r'(?:my name is|i am|i\'m|this is)\s+([A-Z][a-zA-Z0-9]{1,20})',
            raw_text, re.IGNORECASE
        )
        if name_match and not agent_name:
            cand = name_match.group(1).strip()
            if cand.lower() not in {"calling", "here", "an", "a", "the", "ai", "virtual"}:
                resolved_agent = cand

        # ── 2. Extract Business Name ────────────────────────────────────────
        biz_match = re.search(
            r'(?:(?:Business|Company|Organization|Campaign)[:\s]+([A-Za-z0-9\s&\-]{2,40}))|(?:(?:calling from|on behalf of|from)\s+([A-Z][A-Za-z0-9\s&\-]{2,35}))',
            raw_text, re.IGNORECASE
        )
        is_default_biz = not business_name or business_name.strip().lower() in {"default business", "default_business", "our organization", "our company", "our team"}
        if biz_match and is_default_biz:
            cand = (biz_match.group(1) or biz_match.group(2) or "").strip().rstrip(".,!")
            stop = {"a", "an", "the", "our", "my", "this", "us", "today", "here", "you", "your", "default business"}
            if cand and cand.lower() not in stop:
                resolved_business = cand
        elif is_default_biz:
            resolved_business = "our company"

        # ── 3. Extract Objective ────────────────────────────────────────────
        obj_match = re.search(
            r'(?:objective|goal|purpose|aim)[:\s]+([^\.\n]{10,150})',
            raw_text, re.IGNORECASE
        )
        objective = obj_match.group(1).strip() if obj_match else ""

        # ── 4. Extract Outbound Purpose ─────────────────────────────────────
        purpose_match = re.search(
            r'(?:purpose|calling to|calling regarding|calling about|reason for call)[:\s]+([^\.\n]{10,120})',
            raw_text, re.IGNORECASE
        )
        outbound_purpose = (
            purpose_match.group(1).strip() if purpose_match
            else (objective or "sharing information and assisting you with our services")
        )

        # ── 5. Extract Greeting / Opening ───────────────────────────────────
        from app.services.ai.campaign_greeting_service import CampaignGreetingService
        extracted_greeting = CampaignGreetingService.extract_greeting_from_script(
            script_text=raw_text,
            agent_name=resolved_agent,
            company_name=resolved_business,
        )
        if extracted_greeting:
            greeting = extracted_greeting
        else:
            greeting_match = re.search(
                r'["\']?((?:Hi|Hello|Good morning|Good afternoon|Vanakkam|Namaste)[^\n"\']{5,150}(?:\?|!))',
                raw_text, re.IGNORECASE
            )
            greeting = (
                greeting_match.group(1).strip() if greeting_match
                else f"Hello! My name is {resolved_agent} calling from {resolved_business}."
            )

        # ── 6. Extract Conversation Steps ───────────────────────────────────
        steps = cls._extract_steps(raw_text)

        # ── 7. Extract Global Objection Section ─────────────────────────────
        global_objections, post_objection_rule = cls._extract_objections(raw_text)

        # ── 8. Extract Closing ───────────────────────────────────────────────
        closing_match = re.search(
            r'(?:closing|conclusion|wrap.?up|farewell|thank you for your time)[:\s]+([^\n]{10,200})',
            raw_text, re.IGNORECASE
        )
        closing = closing_match.group(1).strip() if closing_match else "Thank you for your time! Have a great day."

        # ── 9. Add global objections to each step for quick lookup ───────────
        for step in steps:
            step.objections = step.objections + global_objections

        return NormalizedScript(
            agent_name=resolved_agent,
            business_name=resolved_business,
            outbound_purpose=outbound_purpose,
            objective=objective,
            greeting=greeting,
            steps=steps if steps else cls._default_steps(),
            closing_message=closing,
            global_objections=global_objections,
            post_objection_rule=post_objection_rule,
            raw_text=raw_text,
        )

    # ── Private helpers ──────────────────────────────────────────────────────

    @classmethod
    def _extract_steps(cls, raw_text: str) -> List[ScriptStep]:
        """
        Parses conversation steps from free-form text.
        Recognises patterns like: "Step 1:", "## Stage 2", "1. Title", "Phase 3", etc.
        Also natively supports conversational dialogue tree scripts with "If yes" / "If no" branches.
        """
        header_steps: List[ScriptStep] = []
        lines = raw_text.splitlines()
        current_lines: List[str] = []
        current_title = ""
        step_counter = 0

        step_header_re = re.compile(
            r'^(?:#+\s*)?(?:Stage|Step|Phase|Section|Part|STEP|STAGE)?\s*'
            r'(\d+)[.:\)]\s*([A-Za-z0-9 &/\-\u0B80-\u0BFF]{3,60})',
            re.IGNORECASE
        )

        def _flush(title: str, body: List[str], idx: int) -> Optional[ScriptStep]:
            if not body:
                return None
            body_text = "\n".join(body).strip()
            q = cls._extract_step_question(body_text)
            req = cls._extract_required_field(body_text)
            pos_kw, neg_kw = cls._extract_branch_keywords(body_text)
            step_objs = cls._extract_inline_objections(body_text)
            pos_action, pos_text, neg_action, neg_text = cls._extract_branch_actions(body_text)
            return ScriptStep(
                step_index=idx,
                step_id=f"step_{idx + 1}",
                title=title,
                question_to_ask=q,
                required_field=req,
                raw_instructions=body_text,
                positive_keywords=pos_kw,
                negative_keywords=neg_kw,
                objections=step_objs,
                positive_branch_action=pos_action,
                positive_branch_text=pos_text,
                negative_branch_action=neg_action,
                negative_branch_text=neg_text,
            )

        for line in lines:
            stripped = line.strip()
            m = step_header_re.match(stripped)
            if m:
                if current_title and current_lines:
                    s = _flush(current_title, current_lines, step_counter - 1)
                    if s:
                        header_steps.append(s)
                step_counter += 1
                current_title = m.group(2).strip()
                current_lines = [stripped]
            else:
                if current_title:
                    current_lines.append(stripped)

        if current_title and current_lines:
            s = _flush(current_title, current_lines, step_counter - 1)
            if s:
                header_steps.append(s)

        if len(header_steps) >= 2:
            return header_steps

        # Fall back to conversational branching tree format (If yes / If no)
        if re.search(r'\bIf\s+(?:yes|no)\b', raw_text, re.IGNORECASE):
            tree_steps = cls._extract_conversational_tree_steps(raw_text)
            if tree_steps and len(tree_steps) >= 1:
                return tree_steps

        if header_steps:
            return header_steps
        return cls._default_steps(raw_text)

    @classmethod
    def _extract_conversational_tree_steps(cls, raw_text: str) -> List[ScriptStep]:
        """
        Parses scripts structured as a natural conversational dialogue tree with 'If yes' and 'If no' branches.
        Extracts sequential turns, opening question, and explicit scripted dialogue responses.
        """
        clean_text = re.sub(r'<[^>]+>', '', raw_text).strip()
        obj_match = re.search(r'\b(?:Here\s+are\s+some\s+)?(?:common\s+objections?|objections?)\b', clean_text, re.IGNORECASE)
        dialogue_part = clean_text[:obj_match.start()].strip() if obj_match else clean_text

        first_branch = re.search(r'\bIf\s+(?:yes|no)\b', dialogue_part, re.IGNORECASE)
        if not first_branch:
            return []

        opening_q = dialogue_part[:first_branch.start()].strip()
        if len(opening_q) > 300:
            sentences = re.split(r'(?<=[.?!])\s+', opening_q)
            opening_q = " ".join(sentences[-2:]).strip()

        branches = re.findall(
            r'If\s+(yes|no)\s*[\-\–\:\—]\s*(.*?)(?=\s+If\s+(?:yes|no)\s*[\-\–\:\—]|\Z)',
            dialogue_part,
            re.IGNORECASE | re.DOTALL
        )
        if not branches:
            return []

        no_branches = [b[1].strip() for b in branches if b[0].lower() == 'no']
        yes_branches = [b[1].strip() for b in branches if b[0].lower() == 'yes']
        max_turns = max(len(no_branches), len(yes_branches))
        steps: List[ScriptStep] = []

        for i in range(max_turns):
            neg_text = no_branches[i] if i < len(no_branches) else None
            pos_text = yes_branches[i] if i < len(yes_branches) else None

            if i == 0:
                title = "1. Greeting & Permission"
                q_to_ask = opening_q
            elif i == 1:
                title = "2. Discovery & Interest"
                q_to_ask = None
                if yes_branches and yes_branches[0]:
                    sentences = re.split(r'(?<=[.?!])\s+', yes_branches[0])
                    if sentences and sentences[-1].strip().endswith("?"):
                        q_to_ask = sentences[-1].strip()
            else:
                title = f"{i+1}. Action & Confirmation"
                q_to_ask = None
                if i - 1 < len(yes_branches) and yes_branches[i-1]:
                    sentences = re.split(r'(?<=[.?!])\s+', yes_branches[i-1])
                    if sentences and sentences[-1].strip().endswith("?"):
                        q_to_ask = sentences[-1].strip()

            neg_action = "ask_callback_time" if (neg_text and any(w in neg_text.lower() for w in ["call", "time", "later", "திரும்ப"])) else ("ask_reason" if (neg_text and any(w in neg_text.lower() for w in ["reason", "why", "காரணம்"])) else "close_call")
            pos_action = "advance_script" if i < max_turns - 1 else "close_call"

            steps.append(ScriptStep(
                step_index=i,
                step_id=f"step_{i+1}",
                title=title,
                question_to_ask=q_to_ask,
                raw_instructions=f"YES instruction: {pos_text or ''}\nNO instruction: {neg_text or ''}",
                positive_branch_action=pos_action,
                positive_branch_text=pos_text,
                negative_branch_action=neg_action,
                negative_branch_text=neg_text,
            ))

        return steps

    @classmethod
    def _extract_step_question(cls, body: str) -> Optional[str]:
        """Finds the main question the AI should ask at this step."""
        for line in body.splitlines():
            stripped = line.strip()
            if (stripped.endswith("?")
                    and len(stripped) > 10
                    and not stripped.lower().startswith(("if ", "when ", "then "))):
                return stripped
        return None

    @classmethod
    def _extract_required_field(cls, body: str) -> Optional[str]:
        """Finds required field names from step instructions."""
        m = re.search(r'(?:required|collect|capture|need|ask for)[:\s]+([a-zA-Z_\s]{3,30})', body, re.IGNORECASE)
        return m.group(1).strip().lower().replace(" ", "_") if m else None

    @classmethod
    def _extract_branch_keywords(cls, body: str) -> Tuple[List[str], List[str]]:
        """Extracts positive/negative branch signal keywords from step body."""
        pos = []
        neg = []
        pos_match = re.findall(
            r'(?:if\s+yes|positive|affirmative|interested|agrees?|accept)[^\n.]{0,80}',
            body, re.IGNORECASE
        )
        neg_match = re.findall(
            r'(?:if\s+no|negative|not interested|decline|refuse|reject)[^\n.]{0,80}',
            body, re.IGNORECASE
        )
        for m in pos_match:
            words = re.findall(r'\b[a-z]{3,15}\b', m.lower())
            pos.extend(words[:5])
        for m in neg_match:
            words = re.findall(r'\b[a-z]{3,15}\b', m.lower())
            neg.extend(words[:5])
        return list(set(pos)), list(set(neg))

    @classmethod
    def _extract_branch_actions(cls, body: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        Extracts the positive and negative branch actions and instruction texts from step body.

        Supports patterns like:
          "If yes - continue introduction"
          "If no - when shall I call u back"
          "If yes → proceed to next step"
          "If no → ask callback time"
          "நோ - எப்போது திரும்ப அழைக்கலாம்"

        Returns (pos_action, pos_text, neg_action, neg_text)
        """
        pos_action: Optional[str] = None
        pos_text: Optional[str] = None
        neg_action: Optional[str] = None
        neg_text: Optional[str] = None

        # Pattern: "If yes[./-/→/:]  <instruction>"
        pos_match = re.search(
            r'(?:if\s+yes|if\s+positive|if\s+interested|if\s+agreed?|yes\s+branch|if\s+they\s+say\s+yes)'
            r'[\s\-\–\→\:\.]+(.{5,120})',
            body, re.IGNORECASE
        )
        if pos_match:
            pos_text = pos_match.group(1).strip().rstrip(".,")
            pos_action = cls._classify_branch_action(pos_text)

        # Pattern: "If no[./-/→/:]  <instruction>"
        neg_match = re.search(
            r'(?:if\s+no|if\s+negative|if\s+not\s+interested|if\s+declined?|if\s+refused?|no\s+branch|if\s+they\s+say\s+no)'
            r'[\s\-\–\→\:\.]+(.{5,120})',
            body, re.IGNORECASE
        )
        if neg_match:
            neg_text = neg_match.group(1).strip().rstrip(".,")
            neg_action = cls._classify_branch_action(neg_text)

        if pos_action or neg_action:
            logger.debug(
                f"[ScriptEngine] Branch actions extracted -- "
                f"YES: action={pos_action!r} text={pos_text!r} | "
                f"NO: action={neg_action!r} text={neg_text!r}"
            )

        return pos_action, pos_text, neg_action, neg_text

    @classmethod
    def _classify_branch_action(cls, instruction_text: str) -> str:
        """
        Classifies a raw branch instruction into a normalized action token.
        Entirely derived from the script's own words — no business hardcoding.
        """
        t = instruction_text.lower().strip()

        # Callback / time collection
        if any(w in t for w in ["call back", "callback", "call u back", "when shall", "when to call",
                                  "convenient time", "better time", "reschedule", "திரும்ப அழைக்க",
                                  "எப்போது"]):
            return "ask_callback_time"

        # Send info / WhatsApp / details
        if any(w in t for w in ["whatsapp", "send details", "send info", "forward", "share details",
                                  "mail", "email details", "message you"]):
            return "send_info_request"

        # Close / end call
        if any(w in t for w in ["close", "end call", "thank you for", "goodbye", "bye", "wrap up",
                                  "close the call", "terminate", "நன்றி", "விடை"]):
            return "close_call"

        # Ask reason / objection handling
        if any(w in t for w in ["why", "reason", "concern", "objection", "hesitat", "doubt"]):
            return "ask_reason"

        # Advance to next script step (positive flow)
        if any(w in t for w in ["continue", "proceed", "next step", "advance", "move on",
                                  "introduction", "present", "explain", "tell", "go ahead",
                                  "தொடர", "அடுத்த"]):
            return "advance_script"

        # Repeat / re-explain
        if any(w in t for w in ["repeat", "explain again", "clarify", "re-explain"]):
            return "repeat_current_step"

        # Default: treat as advance
        return "advance_script"

    @classmethod
    def _extract_inline_objections(cls, body: str) -> List[Dict[str, str]]:
        """
        Finds inline objection-response pairs within a step body.
        Matches patterns like:
          "I don't have time."
          Answer: ...
        """
        objections = []
        lines = body.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # An objection line is typically quoted, bold, or starts with "If customer says"
            if (
                (line.startswith('"') or line.startswith("'") or line.lower().startswith("if "))
                and len(line) > 5
                and i + 1 < len(lines)
            ):
                answer_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if re.match(r'^(?:answer|response|reply|say)[:\-\s]', answer_line, re.IGNORECASE):
                    objection_text = line.strip('"\'')
                    response_text = re.sub(r'^(?:answer|response|reply|say)[:\-\s]*', '', answer_line, flags=re.IGNORECASE).strip()
                    objections.append({"objection": objection_text, "response": response_text})
                    i += 2
                    continue
            i += 1
        return objections

    @classmethod
    def _extract_objections(cls, raw_text: str) -> Tuple[List[Dict[str, str]], Optional[str]]:
        """
        Extracts the global objection section from the raw script.
        Supports both quoted numbered lists (1. "I don't have time." Answer: "...")
        and multiline objection/response formats, plus the closing question rule.
        """
        objections: List[Dict[str, str]] = []
        post_objection_rule: Optional[str] = None

        # Check for closing question / post-objection rule
        cq_match = re.search(
            r'(?:Closing\s+Question|After\s+answering\s+any\s+objection)[^\n]*?(?:always\s+ask[:\s]*)?["\u201c]?([^"\u201d\n\r]+\?)["\u201d]?',
            raw_text, re.IGNORECASE
        )
        if cq_match:
            post_objection_rule = cq_match.group(1).strip()

        # Pattern 1: Numbered quoted objection and answer: 1. "I don't have time." Answer: "..."
        p1 = re.compile(
            r'\b\d+[\.\)]\s*["\u201c]([^"\u201d]+)["\u201d]\s*(?:Answer|Response)[:\-\s]*["\u201c]([^"\u201d]+)["\u201d]',
            re.IGNORECASE
        )
        for q, a in p1.findall(raw_text):
            objections.append({"objection": q.strip(), "response": a.strip()})

        # Pattern 2: Multiline or standard list if Pattern 1 found fewer than 2
        if len(objections) < 2:
            obj_section_re = re.search(
                r'(?:common\s+objections?|objection\s+(?:handling|section|responses?)|objections?)[^\n]*\n(.*?)(?=\n#{1,3}\s|PROCESS\s+TO\s+FOLLOW|\Z)',
                raw_text, re.IGNORECASE | re.DOTALL
            )
            if obj_section_re:
                lines = obj_section_re.group(1).splitlines()
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    if not line:
                        i += 1
                        continue
                    if re.search(r'after\s+(?:answering|handling)\s+(?:any\s+)?objection', line, re.IGNORECASE):
                        if not post_objection_rule:
                            post_objection_rule = line
                        i += 1
                        continue
                    if len(line) > 5:
                        answer_parts = []
                        j = i + 1
                        while j < len(lines):
                            next_line = lines[j].strip()
                            if re.match(r'^(?:answer|response|reply|say)[:\-\s]', next_line, re.IGNORECASE):
                                ans = re.sub(r'^(?:answer|response|reply|say)[:\-\s]*', '', next_line, flags=re.IGNORECASE).strip()
                                answer_parts.append(ans)
                                j += 1
                            elif not next_line:
                                j += 1
                            else:
                                break
                        if answer_parts:
                            clean_obj = re.sub(r'^\d+[\.\)]\s*', '', line).strip('"\' \t')
                            objections.append({"objection": clean_obj, "response": " ".join(answer_parts)})
                            i = j
                            continue
                    i += 1

        return objections, post_objection_rule

    @classmethod
    def _default_steps(cls, raw_text: str = "") -> List[ScriptStep]:
        """Returns 4-step generic skeleton when step extraction finds nothing."""
        return [
            ScriptStep(step_index=0, step_id="step_1", title="Greeting & Purpose",
                       question_to_ask=None, raw_instructions=raw_text[:300] or "Introduce agent and state call purpose.",
                       positive_branch_action="advance_script", negative_branch_action="ask_callback_time"),
            ScriptStep(step_index=1, step_id="step_2", title="Discovery & Needs",
                       question_to_ask=None, raw_instructions="Understand customer requirements and answer questions.",
                       positive_branch_action="advance_script", negative_branch_action="ask_callback_time"),
            ScriptStep(step_index=2, step_id="step_3", title="Solution & Details",
                       question_to_ask=None, raw_instructions="Provide relevant details and qualify interest.",
                       positive_branch_action="advance_script", negative_branch_action="ask_callback_time"),
            ScriptStep(step_index=3, step_id="step_4", title="Action & Closing",
                       question_to_ask=None, raw_instructions="Confirm next steps and conclude call politely.",
                       positive_branch_action="close_call", negative_branch_action="close_call"),
        ]

    @classmethod
    def _default_script(cls, agent_name: str, business_name: str) -> NormalizedScript:
        return NormalizedScript(
            agent_name=agent_name,
            business_name=business_name,
            outbound_purpose=f"sharing information on behalf of {business_name}",
            greeting=f"Hello! My name is {agent_name} calling from {business_name}. How are you today?",
            steps=cls._default_steps(),
            closing_message="Thank you for speaking with us today! Have a great day.",
            raw_text="",
        )


# ---------------------------------------------------------------------------
# Script Branch Resolver
# ---------------------------------------------------------------------------

# Semantic similarity helpers — no external ML needed for speed
_OBJECTION_SIGNALS = [
    "not interested", "no time", "don't have time", "busy", "can't talk", "call later",
    "call back", "callback", "not now", "bad time", "already have",
    "already using", "have a provider", "not looking", "don't need",
    "no need", "expensive", "costly", "price too high", "can't afford",
    "fee", "fees", "membership fee", "investment", "whatsapp", "send on whatsapp",
    "partner", "spouse", "husband", "wife", "discuss", "think about it",
    "need to think", "not ready", "no budget", "out of budget", "in a meeting",
    "occupied", "not good time", "not the right time", "maybe later", "join later",
    "already joined", "other network", "business network", "networking",
]

_POSITIVE_SIGNALS = [
    "yes", "sure", "ok", "okay", "fine", "good", "great", "interested",
    "tell me more", "sounds good", "go ahead", "please", "i want",
    "i would like", "i'm in", "count me in", "absolutely", "definitely",
]

_NEGATIVE_SIGNALS = [
    "no", "nope", "nah", "not", "never", "don't", "don't want",
    "not interested", "no thanks", "no thank you",
]


class ScriptBranchResolver:
    """
    Dynamically resolves the next script action from:
    - Current script state
    - Detected customer intent (including negative_response / positive_agreement)
    - YES/NO branch actions extracted from the uploaded script
    - Semantic objection matching against script-defined objections
    - Post-objection global rules
    """

    @classmethod
    def resolve_action(
        cls,
        script: NormalizedScript,
        current_step_index: int,
        intent: str,
        collected_entities: Dict[str, Any],
        caller_msg: str,
        recent_bot_msg: Optional[str] = None,
        pending_branch_action: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Returns a resolution dict with keys:
          action, target_step_index, advance_step, requires_rag,
          prompt_instruction, response_text (optional), post_objection_rule (optional)
        """
        active_step = script.get_step_by_index(current_step_index) or (
            script.steps[0] if script.steps else None
        )
        active_title = active_step.title if active_step else "In Progress"
        active_question = active_step.question_to_ask if active_step else None

        # ── Priority 0: Explicit negative/positive branch actions ───────────────
        if intent == "negative_response" and active_step and active_step.negative_branch_action:
            return {
                "action": active_step.negative_branch_action,
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": f"Customer responded negatively. Action: {active_step.negative_branch_action}.",
            }

        # ── Priority 0b: Detect semantic objection ───────────────────────────
        if cls._is_objection(intent, caller_msg):
            matched = cls._match_objection(caller_msg, active_step, script.global_objections)
            if matched:
                instruction = (
                    f"The customer raised an objection: '{caller_msg}'. "
                    f"Use this script-defined response: '{matched['response']}'. "
                    f"After responding, "
                    + (
                        f"follow the global rule: {script.post_objection_rule}"
                        if script.post_objection_rule
                        else f"return to the current script step: {active_title}."
                    )
                )
                rule_text = f" {script.post_objection_rule}" if script.post_objection_rule else ""
                full_obj_resp = f"{matched['response']}{rule_text}".strip()
                return {
                    "action": "handle_script_objection",
                    "advance_step": False,
                    "target_step_index": current_step_index,
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                    "post_objection_rule": script.post_objection_rule,
                    "script_response": matched["response"],
                    "response_text": full_obj_resp,
                }
            else:
                # Unknown objection — use RAG for knowledge + polite continuation
                return {
                    "action": "handle_unknown_objection",
                    "advance_step": False,
                    "target_step_index": current_step_index,
                    "requires_rag": True,
                    "prompt_instruction": (
                        f"The customer raised an objection: '{caller_msg}'. "
                        f"Answer empathetically using any relevant knowledge available. "
                        + (f"Then follow the global rule: {script.post_objection_rule} " if script.post_objection_rule else "")
                        + f"Then return to the current script step: {active_title}."
                    ),
                }

        # ── Priority 1a: Human Agent Transfer Request ────────────────────────
        if intent == "human_transfer_request" or cls._is_transfer_request(caller_msg):
            return {
                "action": "transfer_to_human",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": (
                    "The customer requested to speak with a human agent or representative. "
                    "Acknowledge politely and connect them to a representative."
                ),
                "response_text": "I'd be glad to connect you with a representative. Please hold while I transfer your call.",
            }

        # ── Priority 1: Hard intents ────────────────────────────────────────
        if intent == "agent_identity_confirmation":
            return {
                "action": "answer_identity_confirmation",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": (
                    f"Confirm you are an AI assistant named {script.agent_name} calling from {script.business_name} regarding {script.outbound_purpose}. "
                    f"Then ask the current question: '{active_question or active_title}'."
                ),
            }
        if intent == "agent_identity":
            return {
                "action": "answer_agent_identity",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": (
                    f"State clearly: 'I am {script.agent_name} calling from {script.business_name} regarding {script.outbound_purpose}.' "
                    f"Then ask: '{active_question or active_title}'. Do NOT give a generic fallback."
                ),
            }
        if intent == "business_identity":
            return {
                "action": "answer_business_identity",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": (
                    f"State you are calling on behalf of {script.business_name} regarding {script.outbound_purpose}. "
                    f"Then ask: '{active_question or active_title}'."
                ),
            }
        if intent == "call_purpose":
            return {
                "action": "answer_call_purpose",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": (
                    f"Acknowledge that this is an outbound call from {script.business_name} regarding {script.outbound_purpose}. "
                    f"Then ask: '{active_question or active_title}'."
                ),
            }
        if intent == "customer_not_interested":
            return {
                "action": "close_call_not_interested",
                "advance_step": True,
                "target_step_index": max(len(script.steps) - 1, 0),
                "requires_rag": False,
                "prompt_instruction": (
                    f"The customer is not interested. Acknowledge politely and close: {script.closing_message}"
                ),
            }

        # ── Priority 2: Language switch — handled upstream ──────────────────
        if intent == "language_switch":
            return {
                "action": "language_switch",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": "Switch to the customer's requested language and continue the current step.",
            }

        # ── Priority 3: NEGATIVE RESPONSE — use script-defined NO branch ─────
        if intent == "negative_response":
            neg_action = active_step.negative_branch_action if active_step else None
            neg_text = active_step.negative_branch_text if active_step else None

            if neg_action == "ask_callback_time":
                instruction = (
                    f"The customer said '{caller_msg}' — a negative/no response at step '{active_title}'. "
                    f"Per the Agent Script, ask for a convenient callback time. "
                    + (f"Script instruction: '{neg_text}'." if neg_text else "")
                    + " Keep response to 1 natural sentence."
                )
                return {
                    "action": "ask_callback_time",
                    "advance_step": False,
                    "target_step_index": current_step_index,
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                    "response_text": neg_text,
                }
            elif neg_action == "send_info_request":
                instruction = (
                    f"The customer said '{caller_msg}'. "
                    f"Per the Agent Script, offer to send details. "
                    + (f"Script: '{neg_text}'." if neg_text else "")
                )
                return {
                    "action": "send_info_request",
                    "advance_step": False,
                    "target_step_index": current_step_index,
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                    "response_text": neg_text,
                }
            elif neg_action == "close_call":
                instruction = (
                    f"The customer said '{caller_msg}'. "
                    f"Per the Agent Script, close the call politely: {script.closing_message}"
                )
                return {
                    "action": "close_call_negative",
                    "advance_step": True,
                    "target_step_index": max(len(script.steps) - 1, 0),
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                    "response_text": neg_text or script.closing_message,
                }
            elif neg_action == "ask_reason":
                instruction = (
                    f"The customer said '{caller_msg}'. "
                    f"Per the Agent Script, ask empathetically what the concern is. "
                    + (f"Script: '{neg_text}'." if neg_text else "")
                )
                return {
                    "action": "ask_reason",
                    "advance_step": False,
                    "target_step_index": current_step_index,
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                    "response_text": neg_text,
                }
            else:
                # Fallback: no branch defined in script — ask callback time (universal safe default)
                instruction = (
                    f"The customer responded negatively ('{caller_msg}') at step '{active_title}'. "
                    f"Acknowledge politely and ask when would be a better time to talk or "
                    f"if they would like any information sent to them. "
                    f"Do NOT say 'How can I help you today?'. "
                    f"Keep response to 1 natural sentence."
                )
                return {
                    "action": "handle_negative_no_branch",
                    "advance_step": False,
                    "target_step_index": current_step_index,
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                    "response_text": neg_text,
                }

        # ── Priority 4: GREETING — do not advance step, re-ground in current question ─
        if intent == "greeting":
            return {
                "action": "answer_greeting",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": (
                    f"The customer greeted with '{caller_msg}'. "
                    f"Acknowledge warmly, state that you are {script.agent_name} calling from {script.business_name} regarding {script.outbound_purpose}, "
                    f"and ask the current question: '{active_question or active_title}'."
                ),
            }

        # ── Priority 4.5: QUALIFICATION INQUIRY / ELIGIBILITY CHECK ────────
        if intent in ("qualification_inquiry", "eligibility_inquiry"):
            return {
                "action": "answer_qualification_inquiry",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": True,
                "prompt_instruction": (
                    f"The customer asked about their qualification or eligibility status ('{caller_msg}'). "
                    f"Answer using information from the active script and knowledge base. "
                    f"If qualification depends on remaining uncollected details, explain politely that eligibility depends on those details, "
                    f"and then ask the next required question: '{active_question or active_title}'. "
                    f"Do NOT reset qualification state and do NOT restart the script."
                ),
            }

        # ── Priority 5: POSITIVE RESPONSE — use script-defined YES branch ────
        if intent in ("positive_response", "positive_agreement", "customer_interest"):
            pos_action = active_step.positive_branch_action if active_step else "advance_script"
            pos_text = active_step.positive_branch_text if active_step else None

            next_idx = min(current_step_index + 1, max(len(script.steps) - 1, 0))
            next_step = script.get_step_by_index(next_idx)
            next_title = next_step.title if next_step else "Next Step"
            next_question = next_step.question_to_ask if next_step else None
            step_instructions = next_step.raw_instructions[:400] if next_step else ""

            if pos_action == "advance_script":
                instruction = (
                    f"The customer responded positively ('{caller_msg}'). "
                    f"Transition naturally to the next script step: '{next_title}'. "
                )
                if next_question:
                    instruction += f"Ask: '{next_question}'. "
                elif pos_text:
                    instruction += f"Script instruction: '{pos_text}'. "
                elif step_instructions:
                    instruction += f"Follow: {step_instructions[:200]}. "
                instruction += "Keep response to 1-2 spoken sentences. Do NOT output generic chatbot filler."
                
                return {
                    "action": "advance_script",
                    "advance_step": True,
                    "target_step_index": next_idx,
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                }
            elif pos_action == "close_call":
                return {
                    "action": "close_call_positive",
                    "advance_step": True,
                    "target_step_index": max(len(script.steps) - 1, 0),
                    "requires_rag": False,
                    "prompt_instruction": (
                        f"The customer agreed. Close the call positively: {script.closing_message}"
                    ),
                    "response_text": pos_text or script.closing_message,
                }
            else:
                # Default advance for unknown positive action
                instruction = (
                    f"The customer responded positively. "
                    f"Move to the next script step: '{next_title}'. "
                )
                if next_question:
                    instruction += f"Ask: '{next_question}'."
                instruction += " Keep response to 1-2 sentences."
                return {
                    "action": "advance_script",
                    "advance_step": True,
                    "target_step_index": next_idx,
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                }

        # ── Priority 5: Information provided → collect and advance ───────────
        if intent == "information_provided":
            next_idx = min(current_step_index + 1, max(len(script.steps) - 1, 0))
            next_step = script.get_step_by_index(next_idx)
            next_title = next_step.title if next_step else "Next Step"
            next_question = next_step.question_to_ask if next_step else None

            instruction = (
                f"The customer provided information. Acknowledge briefly and move to: '{next_title}'. "
            )
            if next_question:
                instruction += f"Ask: '{next_question}'."
            return {
                "action": "collect_and_advance",
                "advance_step": True,
                "target_step_index": next_idx,
                "requires_rag": False,
                "prompt_instruction": instruction,
            }

        # ── Priority 6: Side questions / knowledge queries vs. Script Step Answers ───
        if intent in ("pricing_inquiry", "service_inquiry", "general_query", "question", "information_provided"):
            is_knowledge = cls._is_knowledge_inquiry(caller_msg, active_step)
            is_answer = cls._is_script_step_answer(
                caller_msg=caller_msg,
                active_step=active_step,
                collected_entities=collected_entities,
                recent_bot_msg=recent_bot_msg,
                pending_branch_action=pending_branch_action,
            )

            # Dynamic campaign branching target resolution: prefer step's explicit branch target if present
            if active_step and active_step.on_positive_step is not None:
                target_idx = min(active_step.on_positive_step, max(len(script.steps) - 1, 0))
            else:
                target_idx = min(current_step_index + 1, max(len(script.steps) - 1, 0))

            next_step = script.get_step_by_index(target_idx)
            next_title = next_step.title if next_step else "Next Step"
            next_question = next_step.question_to_ask if next_step else None

            if is_answer and not is_knowledge:
                selected_action = "collect_and_advance"
                instruction = (
                    f"The customer provided information for '{active_title}': '{caller_msg}'. "
                    f"Acknowledge briefly and move to the next script step: '{next_title}'."
                    + (f" Ask: '{next_question}'." if next_question else "")
                )
                logger.debug(
                    f"[ScriptResolver] STEP_ANSWER: step_index={current_step_index}, title='{active_title}', "
                    f"intent='{intent}', is_script_answer={is_answer}, is_knowledge_inquiry={is_knowledge}, "
                    f"selected_action='{selected_action}', target_step_index={target_idx}, rag_resume_step='{active_title}'"
                )
                return {
                    "action": selected_action,
                    "advance_step": True,
                    "target_step_index": target_idx,
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                }

            if is_knowledge:
                # Genuine Knowledge Inquiry -> Use RAG, keep advance_step=False, preserve active_title
                selected_action = "knowledge_lookup"
                logger.debug(
                    f"[ScriptResolver] KNOWLEDGE_QUERY: step_index={current_step_index}, title='{active_title}', "
                    f"intent='{intent}', is_script_answer={is_answer}, is_knowledge_inquiry={is_knowledge}, "
                    f"selected_action='{selected_action}', target_step_index={current_step_index}, rag_resume_step='{active_title}'"
                )
                return {
                    "action": selected_action,
                    "advance_step": False,
                    "target_step_index": current_step_index,
                    "requires_rag": True,
                    "prompt_instruction": (
                        f"Answer the customer's question: '{caller_msg}' using ASSIGNED AGENT KNOWLEDGE. "
                        f"Be concise and natural. After answering, return to the script step: {active_title}."
                        + (f" Then ask: '{active_question}'." if active_question else "")
                    ),
                }

            # Neither an explicit inquiry nor a strict format match:
            # Caller made a general declarative statement or acknowledged the active step.
            # Do NOT send to RAG. Do NOT say "Answer the customer's question... using ASSIGNED AGENT KNOWLEDGE".
            if active_step and (active_step.question_to_ask or active_step.required_field):
                selected_action = "collect_and_advance"
                instruction = (
                    f"The customer responded for '{active_title}': '{caller_msg}'. "
                    f"Acknowledge briefly and move to the next script step: '{next_title}'."
                    + (f" Ask: '{next_question}'." if next_question else "")
                )
                return {
                    "action": selected_action,
                    "advance_step": True,
                    "target_step_index": target_idx,
                    "requires_rag": False,
                    "prompt_instruction": instruction,
                }
            else:
                return {
                    "action": "continue_script",
                    "advance_step": False,
                    "target_step_index": current_step_index,
                    "requires_rag": False,
                    "prompt_instruction": (
                        f"The customer said: '{caller_msg}'. "
                        f"Acknowledge naturally and continue with the current script step: '{active_title}'."
                        + (f" Ask: '{active_question}'." if active_question else "")
                    ),
                }

        # ── Priority 7: Callback / Appointment ──────────────────────────────
        if intent in ("callback_request", "appointment_request"):
            return {
                "action": "schedule_callback",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": (
                    f"The customer requested a callback or appointment. "
                    f"Ask for a convenient time and confirm. Do not restart the script."
                ),
            }

        # ── Priority 8: Thank you / closing ────────────────────────────────
        if intent == "thank_you":
            return {
                "action": "acknowledge_thanks",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": (
                    f"Acknowledge the customer's thanks warmly and continue with: {active_title}."
                ),
            }

        # ── Priority 9: Unclear / garbled ─────────────────────────────────
        if intent == "unclear_speech":
            return {
                "action": "repeat_request",
                "advance_step": False,
                "target_step_index": current_step_index,
                "requires_rag": False,
                "prompt_instruction": "Ask the caller politely to repeat their statement.",
            }

        # ── Default: Continue current script step ────────────────────────────
        step_instruction_text = active_step.raw_instructions[:300] if active_step else ""
        return {
            "action": "continue_script",
            "advance_step": False,
            "target_step_index": current_step_index,
            "requires_rag": False,
            "prompt_instruction": (
                f"Continue the conversation according to the current script step: '{active_title}'. "
                + (f"Step instructions: {step_instruction_text}. " if step_instruction_text else "")
                + (f"Ask: '{active_question}'." if active_question else "")
            ),
        }

    # ── Semantic helpers ──────────────────────────────────────────────────────

    @classmethod
    def _is_objection(cls, intent: str, caller_msg: str) -> bool:
        """Returns True if the customer message looks like an objection."""
        msg_lower = caller_msg.lower().strip()
        # Even if intent classifier flagged it as negative, check if it's an explicit objection
        if any(sig in msg_lower for sig in _OBJECTION_SIGNALS):
            return True
        if intent in ("customer_not_interested", "negative_response"):
            return False  # Pure 'no' handled via negative branch
        return False

    @classmethod
    def _match_objection(
        cls,
        caller_msg: str,
        active_step: Optional[ScriptStep],
        global_objections: List[Dict[str, str]],
    ) -> Optional[Dict[str, str]]:
        """
        Semantically matches the caller's message against all known objections
        in the active step + global objection section.
        Returns the best matching objection dict or None.
        """
        all_objections = list(global_objections)
        if active_step:
            all_objections = list(active_step.objections) + all_objections

        caller_lower = caller_msg.lower().strip()
        caller_words = set(re.findall(r'\b[a-z]{3,}\b', caller_lower))

        best_match = None
        best_score = 0

        for obj_entry in all_objections:
            obj_text = obj_entry.get("objection", "").lower().strip()
            obj_words = set(re.findall(r'\b[a-z]{3,}\b', obj_text))
            if not obj_words:
                continue
            overlap = len(caller_words & obj_words)
            score = overlap / max(len(obj_words), 1)

            # Substring boost: e.g. "don't have time" or "fees are high"
            if obj_text in caller_lower or any(w in caller_lower for w in obj_words if len(w) >= 4):
                score += 0.25

            if score > best_score and score >= 0.25:
                best_score = score
                best_match = obj_entry

        return best_match

    @classmethod
    def _is_transfer_request(cls, text: str) -> bool:
        """
        Determines whether caller explicitly asks to speak with a human agent or representative.
        Returns False for product/topic inquiries (e.g. 'lump sum benefit') or AI identity questions.
        """
        if not text:
            return False
        user_lower = text.lower().strip()

        # AI identity queries are NOT human expert transfer requests
        ai_identity_patterns = [
            r'\b(?:are\s+you|ur|you\'re|is\s+this)\s+(?:an?\s+)?(?:ai|bot|virtual|automated)\s*(?:agent)?\b',
            r'\b(?:ai\s+agent|bot\s+agent|virtual\s+agent)\b',
            r'\bwho\s+is\s+this\s+agent\b',
        ]
        for p in ai_identity_patterns:
            if re.search(p, user_lower):
                return False

        # Specific negative guards: phrases with "lump sum", "benefit", etc.
        if "lump sum" in user_lower or "benefit" in user_lower:
            return False

        phrases = [
            "talk to human agent", "talk to your agent", "talk to a human agent", "speak to human agent",
            "speak to human", "speak with a person", "transfer call", "transfer my call", "transfer me to human",
            "transfer to human", "transfer to a person", "connect me to human", "connect me to a person",
            "human agent", "real person", "human representative", "live human agent",
            "want a human", "need a human", "talk to a human", "speak with a human",
            "human representative", "talk to real person", "speak to real person",
            "speak to an agent", "talk to an agent", "speak with an agent", "talk with an agent",
            "speak to a representative", "talk to a representative", "want to speak to an agent",
            "want to talk to an agent", "need to speak to an agent", "put me through to an agent",
        ]
        if any(p in user_lower for p in phrases):
            return True

        pattern = r'\b(?:talk|speak|transfer|connect|switch|pass|put)\s+(?:me\s+)?(?:to|with|through\s+to)\s+(?:a\s+|an\s+|the\s+)?(?:human|person|representative|agent|rep|expert|live agent|real person)\b'
        if re.search(pattern, user_lower):
            return True

        return False

    @classmethod
    def _is_knowledge_inquiry(cls, caller_msg: str, active_step: Optional[ScriptStep]) -> bool:
        """
        Determines whether caller_msg is an explicit knowledge/RAG inquiry question
        (e.g., 'know more', 'explain', 'what is', 'how much') rather than an answer to active_step.
        Zero hardcoded campaign values.
        """
        caller_lower = caller_msg.lower().strip()
        if not caller_lower:
            return False

        # Knowledge inquiry signal patterns (require inquiry/question phrasing, not bare option words)
        inquiry_signals = [
            r'\b(?:know\s+more|tell\s+me\s+more|explain|can\s+you\s+explain|could\s+you\s+explain|details\s+of|details\s+about|pricing\s+of|cost\s+of|fee\s+for|what\s+are\s+the\s+benefits|explain\s+benefits)\b',
            r'\b(?:what\s+is|what\s+are|how\s+does|how\s+much|why\s+is|where\s+is|do\s+you\s+offer|is\s+there|are\s+there)\b',
            r'\b(?:question\s+about|inquire\s+about|information\s+on|info\s+about)\b',
        ]
        has_inquiry_signal = any(re.search(pat, caller_lower) for pat in inquiry_signals)

        # Check if caller asks a side question with '?' that does NOT match options in active step
        has_qmark = "?" in caller_msg
        if has_qmark and active_step and active_step.question_to_ask:
            q_options = [w.strip().lower() for w in re.split(r'\b(?:or|vs|\/)\b', active_step.question_to_ask) if len(w.strip()) >= 3]
            if any(opt in caller_lower for opt in q_options if len(opt) >= 4):
                has_qmark = False

        return has_inquiry_signal or has_qmark

    @classmethod
    def _is_script_step_answer(
        cls,
        caller_msg: str,
        active_step: Optional[ScriptStep],
        collected_entities: Dict[str, Any],
        recent_bot_msg: Optional[str] = None,
        pending_branch_action: Optional[str] = None,
    ) -> bool:
        """
        Determines whether caller_msg is an answer to the active script step rather than a knowledge query.
        Inspects active step options, required fields, recent bot question, and input content dynamically.
        Zero hardcoded campaign values.
        """
        if not active_step and not recent_bot_msg and not pending_branch_action:
            return False

        caller_lower = caller_msg.lower().strip()
        if not caller_lower:
            return False

        # Explicit knowledge inquiries are NOT script step answers
        if cls._is_knowledge_inquiry(caller_msg, active_step):
            return False

        # Human agent transfer requests are NOT script step answers
        if cls._is_transfer_request(caller_msg):
            return False

        # Context text across active step and recent bot dialogue
        step_title = active_step.title.lower() if active_step else ""
        req_field = (active_step.required_field or "").lower() if active_step else ""
        q_text = (active_step.question_to_ask or "").lower() if active_step else ""
        raw_text = (active_step.raw_instructions or "").lower() if active_step else ""
        recent_bot_lower = (recent_bot_msg or "").lower()
        context_text = f"{step_title} {req_field} {q_text} {raw_text} {recent_bot_lower}"

        # 1. Option / Choice overlap check: if active step or recent bot presents options and caller selects one
        options_text = f"{q_text} {raw_text} {recent_bot_lower}"
        if options_text:
            options = [opt.strip() for opt in re.split(r'\b(?:or|vs|\/)\b', options_text) if len(opt.strip()) >= 3]
            for opt in options:
                clean_opt = re.sub(r'[^a-z0-9\s]+', '', opt).strip()
                if len(clean_opt) >= 3:
                    opt_words = set(clean_opt.split())
                    caller_words = set(caller_lower.split())
                    if opt_words and len(opt_words & caller_words) >= 1:
                        return True

        # 2. Time, Date, Callback & Schedule Satisfaction Check
        # Supports answers like: "5 pm", "3 pm", "tomorrow at 5", "morning", "after 6", "10:30 am", "next Monday", etc.
        time_date_pattern = r'\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|(?:at|after|before|around)\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?|morning|afternoon|evening|night|noon|tomorrow|today|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|நாளை|இன்று|காலை|மாலை)\b'
        has_time_date_expr = bool(re.search(time_date_pattern, caller_lower))
        time_keywords = [
            "time", "date", "callback", "call back", "appointment", "slot", "schedule", "when", "day",
            "visit", "reach", "contact", "convenient", "prefer", "available", "திரும்ப அழைக்க", "எப்போது", "நேரம்"
        ]
        is_time_context = (
            pending_branch_action in ("ask_callback_time", "schedule_callback")
            or any(kw in context_text for kw in time_keywords)
        )
        if has_time_date_expr and is_time_context:
            return True
        if (collected_entities.get("time") or collected_entities.get("date") or collected_entities.get("callback_time")) and is_time_context:
            return True

        # 3. Email Address Satisfaction Check
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        spoken_email_pattern = r'\b[a-z0-9._%+-]+\s*(?:@|at)\s*[a-z0-9.-]+\s*(?:\.|dot)\s*[a-z]{2,}\b'
        has_email = bool(re.search(email_pattern, caller_lower) or re.search(spoken_email_pattern, caller_lower) or "@" in caller_msg or collected_entities.get("email"))
        is_email_context = any(kw in context_text for kw in ["email", "mail", "id", "address"])
        if has_email and (is_email_context or "email" in step_title or "mail" in step_title or "email" in req_field):
            return True

        # 4. Numeric / Phone Number Satisfaction Check
        has_digits = bool(re.search(r'\b\d{3,}\b', caller_lower) or re.search(r'\b\d+\b', caller_lower) or collected_entities.get("phone"))
        is_numeric_context = any(kw in context_text for kw in ["phone", "contact", "mobile", "number", "age", "budget", "zip", "pincode", "count", "size", "bhk"])
        if has_digits and is_numeric_context:
            return True

        # 5. Name Satisfaction Check
        is_name_context = any(kw in context_text for kw in ["name", "customer", "patient", "student", "client", "who", "பெயர்"])
        if is_name_context:
            has_name = bool(collected_entities.get("name"))
            is_short_phrase = len(caller_lower.split()) <= 4 and not any(w in caller_lower.split()[0] for w in ["what", "why", "how", "who", "where", "can", "could"])
            if has_name or is_short_phrase:
                return True

        # 6. Direct Answer to Active Step Question or Recent Bot Question
        has_active_question = bool(active_step and active_step.question_to_ask) or bool(recent_bot_msg and "?" in recent_bot_msg)
        if has_active_question and len(caller_lower) >= 1:
            if not ("?" in caller_msg or cls._is_knowledge_inquiry(caller_msg, active_step)):
                return True

        # 7. Collection Step Check (e.g. "Collect: Property / Project", "Location Preference")
        if active_step and (active_step.required_field or any(kw in step_title for kw in ["address", "location", "property", "project", "preference", "budget", "type", "details"])):
            if len(caller_lower) >= 1 and not ("?" in caller_msg or cls._is_knowledge_inquiry(caller_msg, active_step)):
                return True

        return False
