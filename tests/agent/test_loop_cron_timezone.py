from pathlib import Path
from unittest.mock import MagicMock

from auto_cut_bot.agent.loop import AgentLoop
from auto_cut_bot.agent.tools.cron import CronTool
from auto_cut_bot.bus.queue import MessageBus
from auto_cut_bot.cron.service import CronService


def test_agent_loop_registers_cron_tool_with_configured_timezone(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        cron_service=CronService(tmp_path / "cron" / "jobs.json"),
        timezone="Asia/Shanghai",
    )

    cron_tool = loop.tools.get("cron")

    assert isinstance(cron_tool, CronTool)
    assert cron_tool._default_timezone == "Asia/Shanghai"
