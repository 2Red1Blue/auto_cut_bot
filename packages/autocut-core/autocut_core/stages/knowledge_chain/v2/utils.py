"""知识链v2通用工具函数"""

import re
from collections.abc import Mapping

from .types import EventCard


def _levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def _event_sort_key(event: EventCard) -> tuple[int, float]:
    episode = event.get("ep", event.get("episode", 0))
    start_time = event.get("start_time", 0)
    return episode, float(start_time)


def build_short_id_map(events: list[EventCard], prefix: str = "E") -> dict[str, str]:
    short_to_full: dict[str, str] = {}
    sorted_events = sorted(events, key=_event_sort_key)
    for idx, event in enumerate(sorted_events, start=1):
        full_id = event.get("id")
        if not isinstance(full_id, str) or not full_id:
            continue
        for p in (prefix.upper(), prefix.lower()):
            short_to_full[f"{p}{idx}"] = full_id
            short_to_full[f"{p}{idx:02d}"] = full_id
            short_to_full[f"{p}p{idx}"] = full_id
            short_to_full[f"{p}p{idx:02d}"] = full_id
    return short_to_full


def map_short_id(short_id: str, id_map: Mapping[str, str]) -> str | None:
    if not short_id:
        return None
    sid = str(short_id).strip()
    if sid in id_map:
        return id_map[sid]
    # 更鲁棒的前缀去除，避免多前缀边界问题
    normalized = sid.upper().strip()
    for prefix in ["EP", "E", "P", "BEAT", "B"]:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    normalized = normalized.lstrip("0")
    if not normalized:
        normalized = "0"
    for key, val in id_map.items():
        key_norm = key.upper().lstrip("E").lstrip("P").lstrip("0")
        if key_norm == normalized:
            return val
    for key, val in id_map.items():
        if _levenshtein(sid.upper(), key.upper()) <= 1:
            return val
    return None


def generate_chapter_id(start_ep: int, end_ep: int) -> str:
    return f"ch{start_ep:02d}-{end_ep:02d}"


def generate_beat_id(chapter_id: str, ep: int, phase: str, seq: int) -> str:
    return f"beat-{chapter_id}-ep{ep:02d}-{phase}-{seq}"


def generate_char_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9一-鿿㐀-䶿぀-ヿ가-힯]", "-", name.lower()).strip("-")
    return f"char-{slug}"


def generate_rel_id(char_a: str, char_b: str) -> str:
    return f"rel-{char_a.replace('char-', '')}-{char_b.replace('char-', '')}"


def generate_fact_id(idx: int) -> str:
    return f"fact-{idx:03d}"


def generate_question_id(idx: int) -> str:
    return f"q-{idx:03d}"


def exact_name_match(
    name1: str, name2: str, aliases1: list[str] | None = None, aliases2: list[str] | None = None
) -> bool:
    aliases1 = aliases1 or []
    aliases2 = aliases2 or []
    all_names1 = set([name1.lower()] + [a.lower() for a in aliases1])
    all_names2 = set([name2.lower()] + [a.lower() for a in aliases2])
    return len(all_names1.intersection(all_names2)) > 0


def phase_sort_key(phase: str) -> int:
    phase_order = {
        "setup": 0,
        "escalation": 1,
        "turn": 2,
        "reveal": 3,
        "payoff": 4,
        "consequence": 5,
        "coda": 6,
    }
    return phase_order.get(phase, 99)
