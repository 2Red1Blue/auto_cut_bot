#!/usr/bin/env python3.13
"""精确定位 position 7-12 的编码错误"""

import json

# 重现错误信息中的位置 7-12
def analyze_position():
    """分析 position 7-12 的含义"""
    
    # 用户发送的是 "你是谁" (3个字符)
    user_message = "你是谁"
    
    # 测试不同的字符串组合，看哪个会在 position 7-12 有中文
    test_cases = [
        ("原始消息", user_message),
        ("带 content 前缀", f"content:{user_message}"),
        ("带 message 前缀", f"message:{user_message}"),
        ("JSON 格式", json.dumps({"content": user_message})),
        ("完整消息对象", json.dumps({"role": "user", "content": user_message})),
        ("HTTP 请求体片段", f'{{"content":"{user_message}"}}'),
        ("带空格的字段", f"content: {user_message}"),
        ("带引号", f'"content":"{user_message}"'),
    ]
    
    print("分析 position 7-12 可能的字符串:\n")
    
    for name, text in test_cases:
        if len(text) > 7:  # 至少有 8 个字符才可能触发 position 7 的错误
            # 检查 position 7-12 附近的字符
            chars_at_7_12 = text[7:13] if len(text) > 12 else text[7:]
            has_non_ascii = any(ord(c) > 127 for c in chars_at_7_12)
            
            if has_non_ascii:
                print(f"✓ {name}")
                print(f"  字符串: {text!r}")
                print(f"  长度: {len(text)}")
                print(f"  Position 7-12: {chars_at_7_12!r}")
                print(f"  完整位置分布:")
                for i, c in enumerate(text):
                    marker = " ← 非ASCII" if ord(c) > 127 else ""
                    if i >= 5 and i <= 13:  # 只显示 5-13 附近
                        print(f"    [{i}] = {c!r} (U+{ord(c):04X}){marker}")
                print()

if __name__ == "__main__":
    analyze_position()
