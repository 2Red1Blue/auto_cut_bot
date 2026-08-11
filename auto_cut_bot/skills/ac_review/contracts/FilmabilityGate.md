# FilmabilityGate — 可拍摄性门控

确保所有素材引用真实存在且可用。

## 1. 素材引用存在性

```sql
SELECT beat_id, source_refs FROM beats WHERE book_id=$1 AND source_refs IS NOT NULL;
```

- [ ] 每个 source_ref 在 source_materials 表中存在: critical
- [ ] source_ref 格式正确: critical

## 2. 素材时长充足性

```sql
SELECT b.beat_id, b.duration AS required, SUM(s.duration) AS available
FROM beats b LEFT JOIN source_refs sr ON b.beat_id=sr.beat_id
LEFT JOIN source_materials s ON sr.source_id=s.id
WHERE b.book_id=$1 GROUP BY b.beat_id, b.duration;
```

- [ ] available >= required * 0.8: critical
- [ ] 无素材的 beat: warning

## 3. 素材质量

```sql
SELECT s.id, s.resolution FROM source_materials s
JOIN source_refs sr ON s.id=sr.source_id WHERE sr.book_id=$1;
```

- [ ] 分辨率 >= 720p: warning
- [ ] 音频质量标记为 good/excellent: warning
