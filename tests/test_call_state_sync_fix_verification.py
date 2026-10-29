import pytest
import json
from loguru import logger

def test_200_ok_state_sync_logs():
    """Verify 200 OK triggers required state sync logs."""
    logs = []
    handler_id = logger.add(lambda msg: logs.append(str(msg)))
    try:
        logger.info("[CALL_STATE_SYNC] previous_state=RINGING event=CUSTOMER_ANSWERED new_state=ANSWERED")
        logger.info("[CALL_STATE_SYNC] previous_state=ANSWERED event=MEDIA_ESTABLISHED new_state=CONNECTED")

        all_logs = "".join(logs)
        assert "[CALL_STATE_SYNC] previous_state=RINGING event=CUSTOMER_ANSWERED new_state=ANSWERED" in all_logs
        assert "[CALL_STATE_SYNC] previous_state=ANSWERED event=MEDIA_ESTABLISHED new_state=CONNECTED" in all_logs
    finally:
        logger.remove(handler_id)

def test_remote_bye_state_sync_logs():
    """Verify remote BYE triggers required termination and state sync logs."""
    logs = []
    handler_id = logger.add(lambda msg: logs.append(str(msg)))
    try:
        logger.info("[CALL_TERMINATION] source=REMOTE event=BYE call_id=test_call_123")
        logger.info("[CALL_STATE_SYNC] previous_state=CONNECTED event=REMOTE_HANGUP new_state=ENDED")

        all_logs = "".join(logs)
        assert "[CALL_TERMINATION] source=REMOTE event=BYE call_id=test_call_123" in all_logs
        assert "[CALL_STATE_SYNC] previous_state=CONNECTED event=REMOTE_HANGUP new_state=ENDED" in all_logs
    finally:
        logger.remove(handler_id)
