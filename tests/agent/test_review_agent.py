
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from auto_cut_bot.agent.state_graph.entities import NodeType
from auto_cut_bot.agent.plugins.review_agent_plugin import ReviewAgentPlugin


class TestReviewAgentPlugin:
    async def test_can_handle_review_gate(self):
        plugin = ReviewAgentPlugin()
        assert plugin.can_handle("review_gate") is True
        assert plugin.can_handle("sub_agent") is False

    async def test_execute_without_book_id(self):
        plugin = ReviewAgentPlugin()
        result = await plugin.execute({})
        assert result.status == "failed"

    async def test_execute_approved(self):
        """Approved review returns completed."""
        plugin = ReviewAgentPlugin()
        verdict = '{"status": "approved", "score": 95, "reasons": []}'
        
        with patch("auto_cut_bot.agent.subagent.SubagentManager") as mock_mgr:
            mock_instance = MagicMock()
            mock_instance.run_inline = AsyncMock(return_value=verdict)
            mock_mgr.return_value = mock_instance
            
            with patch("auto_cut_bot.agent.tools.context.current_request_context") as mock_ctx:
                mock_ctx.return_value = MagicMock(
                    runtime=MagicMock(), channel="test", chat_id="test",
                    session_key="test", message_id="test",
                )
                with patch("auto_cut_bot.security.workspace_access.current_workspace_scope") as mock_ws:
                    mock_ws.return_value = None
                    result = await plugin.execute({"book_id": "42000023011", "job_root": "/tmp"})
                    assert result.status == "completed"
                    assert result.output["review"]["status"] == "approved"

    async def test_execute_rejected(self):
        """Rejected review returns waiting_human for HITL."""
        plugin = ReviewAgentPlugin()
        verdict = '{"status": "rejected", "score": 45, "reasons": [{"severity": "critical", "check": "source_refs", "detail": "missing"}]}'
        
        with patch("auto_cut_bot.agent.subagent.SubagentManager") as mock_mgr:
            mock_instance = MagicMock()
            mock_instance.run_inline = AsyncMock(return_value=verdict)
            mock_mgr.return_value = mock_instance
            
            with patch("auto_cut_bot.agent.tools.context.current_request_context") as mock_ctx:
                mock_ctx.return_value = MagicMock(
                    runtime=MagicMock(), channel="test", chat_id="test",
                    session_key="test", message_id="test",
                )
                with patch("auto_cut_bot.security.workspace_access.current_workspace_scope") as mock_ws:
                    mock_ws.return_value = None
                    result = await plugin.execute({"book_id": "42000023011"})
                    assert result.status == "waiting_human"
