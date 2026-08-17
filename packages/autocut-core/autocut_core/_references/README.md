# Reference Data

This directory contains reference/demo domain data for the video narrative editing engine.

## Files

- `editorial-knowledge-base.json` — Generic editorial rules and heuristics for narrative video editing (schema_version: 1.x). Includes pacing, hook, callback, and quality guidelines for vertical short-form drama.
- `editorial-knowledge/*.json` — Genre profiles describing structural conventions for different content types (revenge, romance, identity-reversal, family-conflict, mystery-reveal, supernatural-power). These are examples of the genre profile schema.

## Customization

All data paths are overridable at runtime:

```python
from autocut_core.libs.editorial_knowledge import load_policy
policy = load_policy(path="/path/to/your/custom-editorial-rules.json")
```

Private deployments may load additional proprietary golden cases and internal genre profiles
from external paths configured via environment variables or project config.
