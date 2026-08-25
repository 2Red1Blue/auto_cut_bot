# Design

The runbook is capability-driven rather than aspirational:

```text
branch checkout -> dependency checks -> Podman PostgreSQL -> migrations on a
new empty DB -> Podman FunASR -> existing automated checks
                                      |
                                      +-> real HTTP Pipeline remains denied
                                          until Authority/Profile injection
```

Commands that already exist are executable blocks. Missing capabilities are
listed as entry conditions, never represented by placeholder commands. The
document links to the detailed FunASR contract instead of duplicating model
identity and calibration internals.

Only the runbook, its two index/status links and the directly linked FunASR
probe documentation belong to this change. The pre-existing local
`auto_cut_bot.config.json` modification is not read, staged or committed.
