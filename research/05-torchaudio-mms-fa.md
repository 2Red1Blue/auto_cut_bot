# Research: torchaudio MMS_FA 可用性

- **Query**: .venv-audio-boundary 中是否已安装 torchaudio，MMS_FA 是否可用
- **Scope**: internal
- **Date**: 2026-08-14

## Findings

### 1. torchaudio 已安装 ✅

**位置**: `/Users/liuzx/Code/python/work_ai/auto_cut_bot/.venv-audio-boundary/`

```
torchaudio version: 2.11.0
```

### 2. MMS_FA 可用 ✅

```python
from torchaudio.pipelines import MMS_FA  # OK
```

### 3. forced_align 可用 ✅

```python
from torchaudio.functional import forced_align  # OK
```

### 4. 使用方式参考

torchaudio MMS_FA (Multi-lingual Speech Forced Alignment) 支持:
- 多语言强制对齐（包括中文）
- 字级/音素级时间戳提取
- 基于 wav2vec2 架构

典型用法:
```python
from torchaudio.pipelines import MMS_FA
from torchaudio.functional import forced_align

bundle = MMS_FA
model = bundle.get_model()
# ... 准备 waveform 和 token indices ...
# forced_align 返回字级时间戳
```

### 5. 当前状态

- torchaudio 和 MMS_FA **已安装但未在代码中使用**
- 设计文档 `vad-audio-snap-design.md` 中提到:
  > "torchaudio MMS_FA多语言对齐：对非中文/非FunASR语言支持，零新依赖"
- 当前 VAD venv 中已有依赖，集成无需额外安装

## 注意事项

1. **venv 路径**: 在 `auto_cut_bot/.venv-audio-boundary/`，不在 workspace 根
2. **torchaudio 2.11.0**: 较新版本，MMS_FA API 稳定
3. **与现有 VAD 的关系**: MMS_FA 是 forced alignment（需要已知文本），不是 VAD（检测语音活动）；两者互补
4. **中文支持**: MMS_FA 支持中文，但 FunASR paraformer 对中文更优（专为中文优化）；MMS_FA 更适合作为非中文语言的 fallback
