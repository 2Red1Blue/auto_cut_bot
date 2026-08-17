# Agent Editing Tools — J-cut/L-cut & B-roll Bridge

These tools are available for the editing agent to invoke **intentionally** when
cinematic techniques improve a clip transition. They are NOT automatic fallbacks
in the snap layer. Default permission: **bypass** (agent may invoke without
human approval).

## Design Principle

The three-tier snap cascade (ASR → VAD → Visual) produces hard cuts that are
physically correct (no word clipping). In 90%+ of cases, a hard cut with
optional 80ms micro-crossfade is sufficient. The tools below are for the
remaining cases where the agent judges that a cinematic edit technique will
produce a better result.

---

## Tool 1: J-cut / L-cut (AV Overlap)

**Identifier**: `right_av_overlap` (strategy in `junction_edits.py`)  
**Permission**: bypass  
**When to use**: When dialogue from the next clip should begin before the video
cuts to it (J-cut), or when dialogue from the current clip continues after the
video cuts away (L-cut). This is a deliberate narrative technique, not a fix
for bad snap points.

**Parameters**:
- `from_clip_id`: Left clip (current)
- `to_clip_id`: Right clip (next)
- `left_video_end_seconds`: Where to cut the left clip's video (audio continues)
- `right_audio_start_seconds`: Where to start the right clip's audio (before video cut)
- `audio_crossfade_ms`: Crossfade duration (recommended: 150-250ms)

**Constraints**:
- Max overlap: 1.2s (hard limit), recommended ≤0.8s
- Max simultaneous speech: 0.1s (avoid double-talk)
- Left clip fade-out: 0.25s; right clip fade-in: 0.05s
- Not allowed on teaser-to-body transitions
- Clips must be from the same episode
- A clip cannot participate in multiple junction edits

**Do NOT use when**:
- The snap point is already clean (no clipping, no jarring transition)
- The two clips have different background noise levels (will produce audible jump)
- There is no narrative reason for audio to lead/lag video
- The agent is "fixing" a snap error — fix the snap instead

---

## Tool 2: B-roll Visual Bridge

**Identifier**: `reviewed_bridge` (strategy in `junction_edits.py`)  
**Permission**: bypass  
**When to use**: When audio must remain continuous across a hard cut (e.g., a
character's sentence spans two camera setups), but the raw cut looks jarring.
The bridge extends audio from the left clip over a short visual transition into
the right clip, effectively creating an L-cut with controlled audio tail.

**Parameters**:
- `from_clip_id`: Left clip
- `to_clip_id`: Right clip
- `left_video_end_seconds`: Where to end left clip video
- `max_audio_tail_seconds`: How long left audio extends over right clip (max 2.0s)
- `left_audio_fade_out_ms`: Fade out duration (default 250ms)

**Constraints**:
- Max audio tail: 2.0s
- Max overlap: 1.2s (hard limit)
- Max simultaneous speech: 0.1s
- Not allowed on teaser-to-body transitions
- Requires operator/agent review (the "reviewed" in reviewed_bridge)
- Must not introduce unrelated visual content (no stock footage insertion)

**Do NOT use when**:
- A simple cross-dissolve transition would suffice
- You need to insert new B-roll footage (that's a different render-stage concern)
- The audio discontinuity is less than 0.3s (use micro-crossfade instead)

---

## Comparison Table

| Scenario | Auto Snap | J/L-cut | B-roll Bridge |
|---|---|---|---|
| Clean word boundary with visual cut | ✅ Hard cut | ❌ | ❌ |
| Word boundary slightly clipped | ✅ +80ms fade | ❌ | ❌ |
| Director-intended audio lead/lag | ❌ | ✅ Agent calls | ❌ |
| Audio spans visual cut, dialogue continues | ❌ | ❌ | ✅ Agent calls |
| Action scene, no dialogue | ✅ Visual snap | ❌ | ❌ |
| Emotional moment needs reaction shot audio | ❌ | ✅ Agent calls | ❌ |

---

## Integration with Snap

The snap layer (`three_tier_snap_start/end` in `asr_anchor.py`) sets flags on
each candidate:
- `snap_start_needs_fade: true` → micro-crossfade (automatic, 80ms)
- If the agent determines a J/L-cut or B-roll bridge is needed at a junction,
  it adds a constraint to the story plan via `compile_story_plan()` in
  `junction_edits.py`, which is then compiled into render artifacts by
  `compile_artifacts()`.

The snap layer NEVER automatically applies J-cuts, L-cuts, or B-roll bridges.
These are agent-only decisions.

---

## Rendering

Transitions (cross-dissolve, flash, blur, dip-to-black) are a render-stage
concern, not junction edits. They are specified in the render recipe and
applied by ffmpeg during final assembly. They do not change clip boundaries.
