# WritingContract — 三道约束

审核故事计划的写作质量。

## 1. 对白约束

**规则**: 对白必须自然、符合角色性格、推动情节。

```sql
SELECT beat_id, character, dialogue FROM beats WHERE book_id=$1 AND dialogue IS NOT NULL;
```

- [ ] 对白长度合理（每句 < 80 字）: warning
- [ ] 对白与角色 profiles 一致: critical
- [ ] 对白推动情节（有明确 purpose）: warning

## 2. 叙事约束

**规则**: 每个 beat 必须有明确的目的。

```sql
SELECT beat_id, purpose FROM beats WHERE book_id=$1;
```

- [ ] 每个 beat 都有 purpose: critical
- [ ] purpose 分布合理（不能全是 exposition）: warning
- [ ] 有明确的冲突和高潮: critical

## 3. 时间线约束

**规则**: 时间线连贯，无矛盾。

```sql
SELECT episode_id, scene_id, beat_id, time_of_day, duration FROM beats WHERE book_id=$1 ORDER BY episode_id, scene_id, beat_id;
```

- [ ] time_of_day 连贯: critical
- [ ] duration 合理（每个 beat 30-120 秒）: warning
- [ ] 总时长符合预期: warning
