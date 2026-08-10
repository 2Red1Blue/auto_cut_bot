#!/usr/bin/env python3.13
"""测试中文消息是否能正常编码和发送给 LLM"""

import asyncio
import sys
from auto_cut_bot.providers import get_provider

async def test_chinese_message():
    """测试发送包含中文的消息"""
    
    # 获取 provider
    provider = await get_provider()
    
    # 构造包含中文的消息
    messages = [
        {"role": "user", "content": "你是谁"},
        {"role": "assistant", "content": "我是一个 AI 助手"},
        {"role": "user", "content": "你好世界"},
    ]
    
    print("测试消息:")
    for msg in messages:
        print(f"  {msg['role']}: {msg['content']}")
    
    print("\n调用 LLM...")
    try:
        result = await provider.chat_completion(messages=messages)
        print(f"\n响应: {result}")
        return True
    except UnicodeEncodeError as e:
        print(f"\n编码错误: {e}")
        print(f"  object: {e.object!r}")
        print(f"  start: {e.start}")
        print(f"  end: {e.end}")
        print(f"  reason: {e.reason}")
        return False
    except Exception as e:
        print(f"\n其他错误: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = asyncio.run(test_chinese_message())
    sys.exit(0 if success else 1)
