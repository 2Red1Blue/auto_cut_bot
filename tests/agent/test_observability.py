
import pytest
from auto_cut_bot.agent.tools.pipeline._observability import ObservabilityDashboard


class TestObservability:
    def test_build_summary(self):
        sessions = [
            type("s",(),{"status":type("st",(),{"value":"completed"})})(),
            type("s",(),{"status":type("st",(),{"value":"completed"})})(),
            type("s",(),{"status":type("st",(),{"value":"failed"})})(),
        ]
        summary = ObservabilityDashboard.build_summary(sessions, {})
        assert summary["total_sessions"] == 3
        assert abs(summary["failure_rate"] - 1/3) < 0.001
        assert summary["completed_sessions"] == 2
        assert summary["failed_sessions"] == 1
