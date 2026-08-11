# EmotionCurve — 情绪曲线

审核情绪曲线的合理性和多样性。

## 1. 情绪多样性

```sql
SELECT emotion, COUNT(*) AS count FROM beats WHERE book_id=$1 AND emotion IS NOT NULL GROUP BY emotion;
```

- [ ] 至少 3 种不同情绪: warning
- [ ] 单一情绪占比不超过 50%: warning

## 2. 高潮分布

```sql
SELECT COUNT(*) FROM beats WHERE book_id=$1 AND emotion IN ('climax','peak','intense');
```

- [ ] 高潮占比 10-30%: warning
- [ ] 高潮分布均匀（不集中在开头/结尾）: warning

## 3. 情绪过渡

```sql
SELECT episode_id, scene_id, beat_id, emotion FROM beats WHERE book_id=$1 AND emotion IS NOT NULL ORDER BY episode_id, scene_id, beat_id;
```

- [ ] 相邻 beat 情绪差异不大: warning
- [ ] 有合理的过渡 beat: info
