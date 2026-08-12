"""AutoCut Core - 核心业务逻辑层

本模块提供视频自动剪辑的核心功能，包括：
- 脚本解析
- 场景识别
- 剪辑规划
- 质量审核

架构层次：
- agent: Agent 框架（StateGraph 引擎、插件系统）
- pipeline: 流水线阶段和编排
"""

import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

__version__ = "0.1.0"
__all__ = ["logger", "__version__"]
