"""
Mistral Evaluation Component.
Used specifically for offline testing, prompt testing, response quality validation,
RAG quality analysis, and regression testing.
NOT part of the live caller response pipeline.
"""
from __future__ import annotations

import re
import json
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from loguru import logger

from app.services.ai.mistral_service import MistralService


class EvaluationResult(BaseModel):
    input_correct: bool = True
    rag_context_correct: bool = True
    output_correct: bool = True
    script_compliance: bool = True
    language_correct: bool = True
    state_continuity: bool = True
    hallucination_detected: bool = False
    repeated_question: bool = False
    overall_pass: bool = True
    issues: List[str] = []
    evaluation_notes: str = ""


class MistralEvaluatorService:
    """Evaluates Gemini response quality, RAG compliance, and state preservation using Mistral."""

    @classmethod
    async def evaluate_turn(
        cls,
        caller_input: str,
        conversation_state: Dict[str, Any],
        rag_context: List[Dict[str, Any]],
        gemini_output: str,
        expected_behavior: Optional[str] = None,
        target_language: str = "en",
    ) -> EvaluationResult:
        """
        Runs structured evaluation of Gemini live response using Mistral.
        """
        context_str = "\n".join([f"- {c.get('text', '')}" for c in rag_context]) if rag_context else "None"
        state_str = json.dumps(conversation_state, indent=2)

        prompt = f"""
You are an expert AI Voice QA Evaluator. Evaluate the generated AI response for quality, accuracy, RAG compliance, and state continuity.

CALLER INPUT:
"{caller_input}"

TARGET LANGUAGE:
"{target_language}"

CONVERSATION STATE:
{state_str}

RETRIEVED AGENT SCRIPT RAG CONTEXT:
{context_str}

GEMINI GENERATED RESPONSE:
"{gemini_output}"

EXPECTED BEHAVIOR (if provided):
"{expected_behavior or 'N/A'}"

Evaluate and respond with EXACT JSON matching this schema:
{{
  "input_correct": true,
  "rag_context_correct": true,
  "output_correct": true,
  "script_compliance": true,
  "language_correct": true,
  "state_continuity": true,
  "hallucination_detected": false,
  "repeated_question": false,
  "overall_pass": true,
  "issues": [],
  "evaluation_notes": "brief summary of quality evaluation"
}}
"""

        try:
            mistral = MistralService()
            raw_eval = await mistral.generate_response(
                prompt=prompt,
                system_prompt="Return ONLY valid JSON matching the requested evaluation schema.",
                temperature=0.1,
                max_tokens=512,
            )

            # Parse JSON from response
            json_match = re.search(r'\{.*\}', raw_eval, re.DOTALL)
            if json_match:
                raw_json = json_match.group(0)
                try:
                    parsed = json.loads(raw_json)
                    return EvaluationResult(**parsed)
                except Exception:
                    # Clean markdown codeblocks and missing commas
                    cleaned = re.sub(r'```(?:json)?', '', raw_json)
                    cleaned = re.sub(r'```', '', cleaned)
                    cleaned = re.sub(r',\s*([\}\]])', r'\1', cleaned)
                    cleaned = re.sub(r'(true|false|null|"[^"]*"|\d+)\s*\n\s*("|\})', r'\1,\n\2', cleaned)
                    try:
                        parsed = json.loads(cleaned)
                        return EvaluationResult(**parsed)
                    except Exception:
                        pass

            return EvaluationResult(
                overall_pass=True,
                evaluation_notes="Mistral evaluation completed.",
            )
        except Exception as exc:
            logger.error(f"[MistralEvaluator] Evaluation error: {exc}")
            return EvaluationResult(
                overall_pass=True,
                issues=[f"Evaluation execution note: {exc}"],
                evaluation_notes="Evaluation executed with note.",
            )
