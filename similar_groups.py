# -*- coding: utf-8 -*-
"""Find nearly identical notes and suggest grouping them as versions.

Scanning never merges automatically: each group must be reviewed first.
Use bidirectional overlap, never intersection divided by the smaller set.
One-way containment mistakes a component pasted into a larger document for
an identical version. In a real library it chained unrelated documents into
a 15-note group. Requiring enough overlap in both directions prevents that.
"""
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime

N = 4               # Four-character grams work for unsegmented Chinese without a tokenizer.
SKETCH_RATE = 96    # Sample per thousand to avoid comparing every pair of notes.
MAX_POSTING = 60    # Ignore fragments appearing in too many notes (generic boilerplate).
MIN_SHARED = 8      # Minimum shared sampled fragments before an exact comparison.

DEFAULTS = {
    "threshold": 0.90,   # Both directions must meet this overlap ratio.
    "maxRatio": 1.5,     # Maximum length ratio between two versions of the same note.
    "minLength": 100,    # Shorter notes need both an identical title and normalized body.
}

RECORD_NAME = "merge-log.json"

# Automatic titles carry no identifying information.
# A real library had 25 unrelated notes with the same automatic title,
# with bodies from 0 to 79 characters; title-only grouping joined them all.
GENERIC_TITLES = {"新备忘录", "新建备忘录", "无标题", "未命名",
                  "new note", "untitled", "note"}


def record_path(lib_dir):
    return os.path.join(lib_dir, RECORD_NAME)


def normalize(text):
    """Normalize whitespace and punctuation before comparing versions."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[，。、；：！？,.;:!?（）()「」『』\"'“”‘’\-—_·…　]", "", text)


def grams(text):
    return {text[i:i + N] for i in range(len(text) - N + 1)} if len(text) >= N else set()


def _sketch(gram_set):
    """Select deterministic samples so identical notes have identical sketches."""
    return {g for g in gram_set
            if int(hashlib.md5(g.encode("utf-8")).hexdigest()[:4], 16) % 1000 < SKETCH_RATE}


def active_body(entry):
    for v in entry.get("versions", []):
        if v.get("v") == entry.get("active"):
            return v.get("content") or ""
    versions = entry.get("versions") or []
    return (versions[-1].get("content") or "") if versions else ""


def entry_date(entry):
    return entry.get("noteDate") or entry.get("updated") or ""


def scan(data, conf=None):
    """Return groups, compared, tooShort, and config without changing data."""
    cfg = dict(DEFAULTS)
    cfg.update(conf or {})
    threshold, max_ratio, min_length = cfg["threshold"], cfg["maxRatio"], cfg["minLength"]

    entries = data.get("prompts", [])
    rows = []
    for e in entries:
        body = active_body(e)
        rows.append({"entry": e, "norm": normalize(body), "raw": body})

    long_rows = [r for r in rows if len(r["norm"]) >= min_length]
    for r in long_rows:
        r["g"] = grams(r["norm"])
        r["s"] = _sketch(r["g"])

    # Use an inverted index to find candidates before exact comparisons.
    postings = defaultdict(list)
    for i, r in enumerate(long_rows):
        for g in r["s"]:
            postings[g].append(i)
    shared = defaultdict(int)
    for idxs in postings.values():
        if len(idxs) > MAX_POSTING:
            continue
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                shared[(idxs[a], idxs[b])] += 1

    pairs = []
    for (a, b), n in shared.items():
        if n < MIN_SHARED:
            continue
        A, B = long_rows[a], long_rows[b]
        la, lb = len(A["norm"]), len(B["norm"])
        if max(la, lb) > max_ratio * min(la, lb):
            continue
        inter = len(A["g"] & B["g"])
        if not inter:
            continue
        score = min(inter / len(A["g"]), inter / len(B["g"]))
        if score >= threshold:
            pairs.append((score, a, b))

    # Join chains: A resembles B and B resembles C, even if A differs from C.
    parent = list(range(len(long_rows)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    buckets = defaultdict(list)
    for i in range(len(long_rows)):
        buckets[find(i)].append(i)

    pair_by_group = defaultdict(list)
    for score, a, b in pairs:
        pair_by_group[find(a)].append((score, a, b))

    groups = []
    for root, members in buckets.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: entry_date(long_rows[i]["entry"]))
        groups.append(_shape(long_rows, members, pair_by_group[root], "body"))

    # Short notes need both identical titles and identical normalized bodies.
    # The same reminder text alone can describe separate notes, not versions.
    # Never group by title alone: unrelated notes often share automatic titles.
    # Empty bodies are skipped as there is no content to make into versions.
    by_key = defaultdict(list)
    for i, r in enumerate(rows):
        if len(r["norm"]) >= min_length:
            continue
        t = (r["entry"].get("title") or "").strip()
        if not t or t.lower() in GENERIC_TITLES:
            continue
        if not r["norm"]:          # Empty notes have no content to group as versions.
            continue
        by_key[(t, r["norm"])].append(i)
    for idxs in by_key.values():
        if len(idxs) < 2:
            continue
        idxs.sort(key=lambda i: entry_date(rows[i]["entry"]))
        groups.append(_shape(rows, idxs, [], "title"))

    groups.sort(key=lambda g: (-len(g["members"]), g["members"][0]["date"]))
    return {"groups": groups,
            "compared": len(long_rows),
            "tooShort": len(rows) - len(long_rows),
            "config": cfg}


def _shape(rows, members, group_pairs, reason):
    index = {m: k for k, m in enumerate(members)}
    scores = []
    for score, a, b in sorted(group_pairs, reverse=True):
        if a in index and b in index:
            scores.append({"a": index[a], "b": index[b], "score": round(score, 3)})
    return {
        "reason": reason,          # body = near-identical bodies; title = identical short title/body pairs.
        "members": [{
            "id": rows[m]["entry"]["id"],
            "title": rows[m]["entry"].get("title", ""),
            "project": rows[m]["entry"].get("project", ""),
            "date": entry_date(rows[m]["entry"]),
            "length": len(rows[m]["norm"]),
            "versions": len(rows[m]["entry"].get("versions", [])),
            "preview": rows[m]["raw"][:400],
            "body": rows[m]["raw"],
        } for m in members],
        "scores": scores,
    }


def merge(data, group_ids, lib_dir):
    """Merge selected groups, ordered by note date from oldest to newest.

    Keep the newest entry's ID, title, project, and source note ID so later
    organization and push operations still address the newest original note.
    Store complete original entries in the log for lossless undo.
    """
    by_id = {e["id"]: e for e in data.get("prompts", [])}
    undo_groups = []
    merged_ids = set()

    for ids in group_ids:
        picked = [by_id[i] for i in ids if i in by_id and i not in merged_ids]
        if len(picked) < 2:
            continue
        picked.sort(key=entry_date)
        keeper = picked[-1]
        undo_groups.append({
            "keeperId": keeper["id"],
            "before": [json.loads(json.dumps(e, ensure_ascii=False)) for e in picked],
        })
        versions = []
        for n, e in enumerate(picked, 1):
            versions.append({"v": n, "content": active_body(e),
                             "time": entry_date(e) or e.get("updated", "")})
        keeper["versions"] = versions
        keeper["active"] = len(versions)
        keeper["updated"] = datetime.now().isoformat(timespec="seconds")
        for e in picked[:-1]:
            merged_ids.add(e["id"])

    if not undo_groups:
        return data, {"groups": 0, "removed": 0}

    data["prompts"] = [e for e in data.get("prompts", []) if e["id"] not in merged_ids]
    # Preserve importedNoteIds: the original notes still exist in Apple Notes.
    # Removing these IDs would import the merged-away notes again next time.
    _write_record(lib_dir, undo_groups)
    return data, {"groups": len(undo_groups), "removed": len(merged_ids)}


def _write_record(lib_dir, undo_groups):
    os.makedirs(lib_dir, exist_ok=True)
    payload = {"time": datetime.now().isoformat(timespec="seconds"), "groups": undo_groups}
    tmp = record_path(lib_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, record_path(lib_dir))


def load_record(lib_dir):
    try:
        with open(record_path(lib_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def undo(data, lib_dir):
    """Undo the whole last merge batch, then remove its one-use record."""
    record = load_record(lib_dir)
    if not record or not record.get("groups"):
        return data, {"restored": 0, "error": "There is no merge to undo."}

    keepers = {g["keeperId"] for g in record["groups"]}
    kept = [e for e in data.get("prompts", []) if e["id"] not in keepers]
    restored = 0
    for g in record["groups"]:
        for e in g["before"]:
            kept.append(e)
            restored += 1
    data["prompts"] = kept
    try:
        os.remove(record_path(lib_dir))
    except OSError:
        pass
    return data, {"restored": restored, "groups": len(record["groups"])}
