"""测试 Ark (火山方舟) Responses API + Files API 支持情况。

测试 3 件事:
1. litellm.responses() 通过 openai/<model> + 自定义 api_base 转发到 Ark
2. litellm.create_file() / httpx 上传视频文件到 Ark Files API
3. litellm.responses() 使用 file_id 引用上传的视频
"""

import os, sys, json, traceback
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent.parent / "ac_auto_cut" / ".env")

API_BASE = "https://ark.cn-beijing.volces.com/api/v3"
API_KEY = os.environ.get("ARK_API_KEY", "")
MODEL = "doubao-seed-2-1-pro-260628"
TEST_VIDEO = "/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs/when-lucifer-kneels/window-assets/source-010/source-010-w001-480p.mp4"

if not API_KEY:
    print("❌ ARK_API_KEY 未设置，退出")
    sys.exit(1)

print(f"API Base: {API_BASE}")
print(f"Model:    {MODEL}")
print(f"API Key:  {API_KEY[:8]}...")
print(f"Video:    {Path(TEST_VIDEO).name} ({Path(TEST_VIDEO).stat().st_size / 1024 / 1024:.1f} MB)")
print()

import litellm

# ═══════════════════════════════════════════════════════════════════════
# 测试 1: litellm.responses() — Responses API
# ═══════════════════════════════════════════════════════════════════════
print("=" * 60)
print("测试 1: litellm.responses() — Responses API 转发到 Ark")
print("=" * 60)

try:
    resp = litellm.responses(
        model=f"openai/{MODEL}",
        input="回复两个字：成功",
        api_base=API_BASE,
        api_key=API_KEY,
        max_output_tokens=20,
    )
    print(f"✅ Responses API 调用成功!")
    print(f"   类型: {type(resp).__name__}")
    if hasattr(resp, 'output'):
        print(f"   输出: {str(resp.output)[:200]}")
    else:
        print(f"   原始: {str(resp)[:300]}")
except Exception as e:
    print(f"❌ 失败: {type(e).__name__}: {e}")

print()

# ═══════════════════════════════════════════════════════════════════════
# 测试 2: 上传视频文件 — 先试 litellm.create_file()，失败降级 httpx
# ═══════════════════════════════════════════════════════════════════════
print("=" * 60)
print("测试 2: 上传视频到 Ark Files API")
print("=" * 60)

file_id = None

# 方式 A: litellm.create_file()
print("  → 方式 A: litellm.create_file()")
try:
    with open(TEST_VIDEO, "rb") as f:
        resp = litellm.create_file(
            file=f,
            purpose="assistants",
            custom_llm_provider="openai",
            api_base=API_BASE,
            api_key=API_KEY,
        )
    print(f"  ✅ litellm.create_file 成功!")
    if hasattr(resp, 'id'):
        file_id = resp.id
    elif isinstance(resp, dict):
        file_id = resp.get("id")
    print(f"  file_id: {file_id}")
    print(f"  完整响应: {resp}")
except Exception as e:
    print(f"  ❌ litellm.create_file 失败: {type(e).__name__}: {e}")

# 方式 B: httpx 直调
if not file_id:
    print("  → 方式 B: httpx 直调 Ark /v1/files")
    try:
        import httpx
        with open(TEST_VIDEO, "rb") as f:
            r = httpx.post(
                f"{API_BASE}/files",
                headers={"Authorization": f"Bearer {API_KEY}"},
                files={"file": ("source-010-w001-480p.mp4", f, "video/mp4")},
                data={"purpose": "assistants"},
                timeout=120,
            )
        print(f"  状态码: {r.status_code}")
        print(f"  响应: {r.text[:500]}")
        if r.status_code == 200:
            data = r.json()
            file_id = data.get("id")
            print(f"  ✅ file_id: {file_id}")
    except Exception as e:
        print(f"  ❌ httpx 也失败: {e}")

print()

# ═══════════════════════════════════════════════════════════════════════
# 测试 3: 用 file_id 调 Responses API 分析视频
# ═══════════════════════════════════════════════════════════════════════
print("=" * 60)
print("测试 3: 用 file_id 调 Responses API 分析视频内容")
print("=" * 60)

if not file_id:
    print("⏭️  跳过 — 测试 2 未获得 file_id")
else:
    try:
        resp = litellm.responses(
            model=f"openai/{MODEL}",
            input=[
                {"role": "user", "content": [
                    {"type": "input_file", "file_id": file_id},
                    {"type": "input_text", "text": "用一句话描述视频中发生了什么。"},
                ]},
            ],
            api_base=API_BASE,
            api_key=API_KEY,
            max_output_tokens=200,
        )
        print(f"✅ 视频分析成功!")
        if hasattr(resp, 'output'):
            print(f"   输出: {str(resp.output)[:500]}")
        else:
            print(f"   原始: {str(resp)[:500]}")
    except Exception as e:
        print(f"❌ 失败: {type(e).__name__}: {e}")
        # 尝试不同的 content 格式
        print()
        print("  → 尝试备选格式: file_id 放在 content 里")
        try:
            resp = litellm.responses(
                model=f"openai/{MODEL}",
                input=[
                    {"role": "user", "content": [
                        {"type": "file", "file_id": file_id},
                        {"type": "text", "text": "用一句话描述视频中发生了什么。"},
                    ]},
                ],
                api_base=API_BASE,
                api_key=API_KEY,
                max_output_tokens=200,
            )
            print(f"  ✅ 备选格式成功!")
            if hasattr(resp, 'output'):
                print(f"  输出: {str(resp.output)[:500]}")
            else:
                print(f"  原始: {str(resp)[:500]}")
        except Exception as e2:
            print(f"  ❌ 备选格式也失败: {type(e2).__name__}: {e2}")

print()
print("=" * 60)
print("测试完成")
print("=" * 60)
