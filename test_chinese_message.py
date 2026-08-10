"""测试 WebSocket 消息处理链路中的编码问题"""
import asyncio
from auto_cut_bot.providers import create_provider
from auto_cut_bot.config import Config

async def test_chinese_message():
    """测试发送中文消息给 LLM"""
    
    # 加载配置
    config = Config.load()
    
    # 创建 provider
    provider = create_provider(config)
    
    # 测试消息
    message = "你是谁"
    
    print(f"Testing message: {message!r}")
    print(f"Provider: {type(provider).__name__}")
    print(f"Model: {config.model}")
    print(f"API Base: {config.api_base}")
    
    try:
        # 直接调用 provider
        response = await provider.generate(
            prompt=message,
            context=[],
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        print(f"✓ Response: {response[:100]}")
        
    except UnicodeEncodeError as e:
        print(f"✗ UnicodeEncodeError: {e}")
        print(f"  encoding: {e.encoding}")
        print(f"  start: {e.start}, end: {e.end}")
        print(f"  object: {e.object!r}")
        import traceback
        traceback.print_exc()
        
    except Exception as e:
        print(f"✗ Error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_chinese_message())
