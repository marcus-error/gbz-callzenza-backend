"""
Regression tests for CallZenza script-state routing:
Verifies that valid answers to active script questions are properly classified
as script step answers (advancing the script) rather than falling through to
RAG/knowledge_lookup or triggering false transfer requests.
"""
import pytest
from app.services.ai.script_engine import (
    NormalizedScript,
    ScriptStep,
    ScriptBranchResolver,
    ScriptNormalizer,
)
from app.services.ai.agent_engine import AgentEngine
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.api.routes.local_voice_test import (
    detect_talk_to_agent_intent,
    extract_requested_agent_name,
)


def _build_test_script():
    return NormalizedScript(
        agent_name="Sarah",
        business_name="CallZenza Realty",
        outbound_purpose="schedule an advisor consultation",
        objective="qualify lead and book callback",
        greeting="Hello, this is Sarah from CallZenza Realty.",
        steps=[
            ScriptStep(
                step_index=0,
                step_id="step_1",
                title="1. Preferred Callback Time",
                question_to_ask="What time works best for an advisor callback?",
                required_field="callback_time",
                raw_instructions="Ask when the customer is free for an advisor callback.",
                positive_branch_action="advance_script",
                negative_branch_action="ask_callback_time",
            ),
            ScriptStep(
                step_index=1,
                step_id="step_2",
                title="2. Email Address",
                question_to_ask="Could you please share your email address?",
                required_field="email",
                raw_instructions="Collect customer email address.",
                positive_branch_action="advance_script",
                negative_branch_action="close_call",
            ),
            ScriptStep(
                step_index=2,
                step_id="step_3",
                title="3. Confirmation",
                question_to_ask="Shall I confirm this appointment for you?",
                required_field="confirmation",
                raw_instructions="Confirm details with the customer.",
                positive_branch_action="close_call",
                negative_branch_action="ask_reason",
            ),
        ],
        closing_message="Thank you for your time. Have a wonderful day!",
    )


class TestScriptStateRouting:

    def test_1_active_callback_time_5pm_recognized_and_advances(self):
        """
        Scenario 1:
        AI: "Got it. What time works best for an advisor callback?"
        Customer: "5 pm"
        Must be recognized as a script answer, advance step, and NOT go to RAG.
        """
        script = _build_test_script()
        intent = AgentEngine.classify_caller_intent("5 pm")

        res = ScriptBranchResolver.resolve_action(
            script=script,
            current_step_index=0,
            intent=intent,
            collected_entities={},
            caller_msg="5 pm",
            recent_bot_msg="Got it. What time works best for an advisor callback?",
        )

        assert res["action"] == "collect_and_advance"
        assert res["advance_step"] is True
        assert res["target_step_index"] == 1
        assert res["requires_rag"] is False
        assert "ASSIGNED AGENT KNOWLEDGE" not in res["prompt_instruction"]
        assert "Answer the customer's question: '5 pm'" not in res["prompt_instruction"]

    def test_2_active_time_question_dynamic_variations(self):
        """
        Scenario 2:
        Must dynamically accept other valid answers:
        "3 pm", "tomorrow at 5", "morning", "after 6", "10:30 am"
        """
        script = _build_test_script()

        test_variations = ["3 pm", "tomorrow at 5", "morning", "after 6", "10:30 am", "at 5"]
        for answer in test_variations:
            intent = AgentEngine.classify_caller_intent(answer)
            res = ScriptBranchResolver.resolve_action(
                script=script,
                current_step_index=0,
                intent=intent,
                collected_entities={},
                caller_msg=answer,
                recent_bot_msg="What time works best for an advisor callback?",
            )

            assert res["action"] == "collect_and_advance", f"Failed for '{answer}': action is {res['action']}"
            assert res["advance_step"] is True, f"Failed for '{answer}': advance_step is False"
            assert res["target_step_index"] == 1, f"Failed for '{answer}': wrong target {res['target_step_index']}"
            assert res["requires_rag"] is False, f"Failed for '{answer}': requires_rag is True"
            assert "ASSIGNED AGENT KNOWLEDGE" not in res["prompt_instruction"]

    def test_3_active_script_question_with_unrelated_knowledge_question(self):
        """
        Scenario 3:
        Active script question + unrelated knowledge inquiry.
        Must use RAG, NOT advance script, and preserve current script step.
        """
        script = _build_test_script()
        query = "Can you explain the pricing and approval details?"
        intent = AgentEngine.classify_caller_intent(query)

        res = ScriptBranchResolver.resolve_action(
            script=script,
            current_step_index=0,
            intent=intent,
            collected_entities={},
            caller_msg=query,
            recent_bot_msg="What time works best for an advisor callback?",
        )

        assert res["action"] == "knowledge_lookup"
        assert res["advance_step"] is False
        assert res["target_step_index"] == 0  # Preserved!
        assert res["requires_rag"] is True
        assert "ASSIGNED AGENT KNOWLEDGE" in res["prompt_instruction"]
        assert "1. Preferred Callback Time" in res["prompt_instruction"]

    def test_4_active_script_question_with_transfer_request(self):
        """
        Scenario 4:
        Active script question + "I want to speak to an agent"
        Must be recognized as transfer, NOT knowledge question or script answer.
        """
        script = _build_test_script()
        transfer_msg = "I want to speak to an agent"

        intent = AgentEngine.classify_caller_intent(transfer_msg)
        assert intent == "human_transfer_request"

        res = ScriptBranchResolver.resolve_action(
            script=script,
            current_step_index=1,
            intent=intent,
            collected_entities={},
            caller_msg=transfer_msg,
        )

        assert res["action"] == "transfer_to_human"
        assert res["advance_step"] is False
        assert res["requires_rag"] is False

        # Also verify local voice test transfer helpers
        assert detect_talk_to_agent_intent(transfer_msg) is True

    def test_5_lump_sum_benefit_not_transfer_or_agent_name(self):
        """
        Scenario 5:
        "I'm looking for lump sum benefit"
        Must NOT be interpreted as a human-agent name or transfer request.
        """
        msg = "I'm looking for lump sum benefit"

        # 1. Transfer intent detector must return False
        assert detect_talk_to_agent_intent(msg) is False

        # 2. Expert name extractor must return None
        assert extract_requested_agent_name(msg) is None

        # 3. ScriptBranchResolver transfer detector must return False
        assert ScriptBranchResolver._is_transfer_request(msg) is False

        # 4. Agent intent classifier must NOT return human_transfer_request
        intent = AgentEngine.classify_caller_intent(msg)
        assert intent != "human_transfer_request"

        # 5. Memory service entity extraction must NOT extract "lump" as target_entity or name
        session_id = "test_lump_sum_session"
        agent_id = "agent_test"
        ConversationMemoryService.clear_session(agent_id, session_id)
        ConversationMemoryService.add_turn(agent_id, session_id, role="user", content=msg)
        entities = ConversationMemoryService.get_entities(agent_id, session_id)
        assert entities.get("target_entity") != "lump"
        assert entities.get("name") != "lump"

    def test_6_existing_valid_script_answers_continue_to_advance(self):
        """
        Scenario 6:
        Existing valid script answers continue to advance correctly:
        - Email address advances email step
        - Agreement ("yes") advances step
        - Phone number advances phone step
        """
        script = _build_test_script()

        # Email step (step 1)
        email_msg = "mugilan@gmail.com"
        email_intent = AgentEngine.classify_caller_intent(email_msg)
        res_email = ScriptBranchResolver.resolve_action(
            script=script,
            current_step_index=1,
            intent=email_intent,
            collected_entities={},
            caller_msg=email_msg,
            recent_bot_msg="Could you please share your email address?",
        )
        assert res_email["action"] == "collect_and_advance"
        assert res_email["advance_step"] is True
        assert res_email["target_step_index"] == 2
        assert res_email["requires_rag"] is False

        # Positive agreement ("yes")
        yes_intent = AgentEngine.classify_caller_intent("yes")
        assert yes_intent == "positive_response"
        res_yes = ScriptBranchResolver.resolve_action(
            script=script,
            current_step_index=0,
            intent=yes_intent,
            collected_entities={},
            caller_msg="yes",
        )
        assert res_yes["action"] == "advance_script"
        assert res_yes["advance_step"] is True
        assert res_yes["target_step_index"] == 1
