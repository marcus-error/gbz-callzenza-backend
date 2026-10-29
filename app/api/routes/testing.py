"""
Testing & Evaluation API Endpoints.
Allows running Gemini input/output testing, RAG retrieval verification,
and Mistral quality evaluation matrix.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.services.ai.agent_engine import AgentEngine
from app.services.ai.mistral_evaluator import MistralEvaluatorService, EvaluationResult

router = APIRouter(prefix="/api/testing", tags=["testing"])


class EvaluateRequest(BaseModel):
    agent_id: str = "default_agent"
    caller_input: str
    expected_behavior: Optional[str] = None
    target_language: str = "en"


class TestMatrixResult(BaseModel):
    scenario: str
    input: str
    gemini_response: str
    latency_ms: int
    evaluation: EvaluationResult


@router.post("/evaluate", response_model=Dict[str, Any])
async def evaluate_gemini_turn(
    body: EvaluateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Executes a Gemini live response turn and evaluates it using Mistral Evaluator."""
    res = await AgentEngine.process_user_query(
        db=db,
        agent_id=body.agent_id,
        message=body.caller_input,
        current_language=body.target_language,
    )

    evaluation = await MistralEvaluatorService.evaluate_turn(
        caller_input=body.caller_input,
        conversation_state=res.entities_captured,
        rag_context=res.retrieved_context,
        gemini_output=res.response,
        expected_behavior=body.expected_behavior,
        target_language=body.language,
    )

    return {
        "caller_input": body.caller_input,
        "gemini_output": res.response,
        "latency_ms": res.latency_ms,
        "retrieved_context": res.retrieved_context,
        "evaluation": evaluation.model_dump(),
    }


@router.get("/matrix", response_model=List[TestMatrixResult])
async def run_test_matrix(db: AsyncSession = Depends(get_db)):
    """Runs the 10 core testing scenarios using Gemini LLM and Mistral Evaluation."""
    scenarios = [
        ("1. Greeting", "Hi", "Initial warm greeting"),
        ("2. General Conversation", "How are you doing today?", "Natural friendly answer"),
        ("3. Business Question", "What home solar services do you provide?", "Answer from Agent Script"),
        ("4. Appointment Intent", "I want to book an appointment with a teacher tomorrow.", "Extract intent and entity"),
        ("5. Follow-up", "Around 2 PM would be great.", "Update appointment time"),
        ("6. Language Switch", "Can you speak in Tamil?", "Switch language to Tamil without RAG"),
        ("7. Tamil Follow-up", "வணக்கம் எப்படி இருக்கீங்க?", "Respond in Tamil preserving state"),
        ("8. Unknown Business Question", "Do you offer quantum computing training?", "Follow fallback policy"),
        ("9. Switch back to English", "Let's speak in English now.", "Switch language back to English"),
        ("10. Multi-entity turn", "I am John Doe living in 90210 with a $250 monthly bill", "Extract all 3 entities"),
    ]

    matrix_results = []
    session_id = "test_matrix_session"

    for name, user_input, expected in scenarios:
        res = await AgentEngine.process_user_query(
            db=db,
            agent_id="default_agent",
            message=user_input,
            session_id=session_id,
        )

        eval_res = await MistralEvaluatorService.evaluate_turn(
            caller_input=user_input,
            conversation_state=res.entities_captured,
            rag_context=res.retrieved_context,
            gemini_output=res.response,
            expected_behavior=expected,
            target_language=res.language,
        )

        matrix_results.append(
            TestMatrixResult(
                scenario=name,
                input=user_input,
                gemini_response=res.response,
                latency_ms=res.latency_ms,
                evaluation=eval_res,
            )
        )

    return matrix_results
