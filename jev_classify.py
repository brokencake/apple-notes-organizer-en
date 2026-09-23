#!/usr/bin/env python3
"""Suggest a project for unfiled notes using TypeSafe's Jev.

This module only ever returns suggestions. Nothing is moved and no note is
edited here; filing still goes through the organizer's own confirm-then-move
flow, exactly as it does when you sort notes by hand.

Why one yes/no question per project instead of "pick the best project":
    A forced single choice has to put every note somewhere, so a note that
    belongs in none of your projects gets pushed into whichever one is least
    wrong. Asking "does this belong in X?" once per project allows two answers
    a single choice cannot express: none of them (genuinely unfiled) and
    several of them (ambiguous, a human should look). Each answer carries a
    probability, and that is the only reason a meaningful line can be drawn
    between "file this automatically" and "ask me".

One note costs one request: every project's question travels in the same
`questions` object and the server evaluates them together.
"""
import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from locale_support import translate

API_URL = os.environ.get("TYPESAFE_API_BASE", "https://api.typesafe.ai") + "/v1/systemone"
MODEL = "jev-latest"
BODY_LIMIT = 4000      # Sending the whole body is unnecessary; the opening is enough to place it.
PREVIEW_LIMIT = 1500   # Returned to your own browser so you can check the call. Not what is sent.
MAX_WORKERS = 6        # Keep concurrency modest to avoid rate limiting.
MAX_ITEMS = 200

CONFIG_NAME = "jev-config.json"

DEFAULTS = {
    "apiKey": "",
    "descriptions": {},     # {project: one sentence describing what it holds}
    "keywords": {},         # {project: [keyword]} — a hit decides it locally, for free
    "neverSend": [],        # Notes folders that are skipped without being read
    "skipDescription": [],  # Projects that need no description (staging, secrets)
    "autoThreshold": 0.75,  # Top score at or above this may be filed automatically
    "doubtThreshold": 0.45, # Top score below this means "leave it unfiled"
    "minGap": 0.15,         # The top score must beat the runner-up by this much
    "useBuiltinSecrets": True,
    "customSecrets": [],
    "sendSensitive": False, # Off: notes that look like credentials are never sent.
                            # Turning it on only lifts the pattern check; neverSend
                            # folders stay excluded either way.
}

# Built-in checks run locally and are enabled by default.
DEFAULT_SECRET_FOLDERS = []
BUILTIN_SECRETS = [
    ('API keys (sk-…)', re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ('TypeSafe keys (apikey_…)', re.compile(r"\bapikey_[A-Za-z0-9]{16,}", re.I)),
    ('Notion tokens', re.compile(r"\b(?:ntn|secret)_[A-Za-z0-9]{16,}")),
    ('GitHub tokens', re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ('AWS access key IDs', re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ('Any 32+ character token', re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")),
    ('National ID numbers', re.compile(r"[1-9]\d{5}(19|20)\d{2}[01]\d[0-3]\d[0-9Xx]")),
    ('US Social Security numbers', re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ('Lines shaped like "password: …"', re.compile(r"(password|passwd|secret|api[_\- ]?key|token|密码)\s*[:：=]", re.I)),
    ('Account and password pairs (email---password)', re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\s*[-—]{2,}")),
]

VERDICT_AUTO = "auto"           # Confident and unambiguous
VERDICT_CONFIRM = "confirm"     # Close call — you decide
VERDICT_UNFILED = "unfiled"     # Belongs in none of your projects
VERDICT_SKIPPED = "skipped"     # Looks sensitive; never sent
VERDICT_NO_KEY = "no-key"       # Needs Jev, but no API key is configured
VERDICT_ERROR = "error"


def config_path(lib_dir):
    return os.path.join(lib_dir, CONFIG_NAME)


def load_conf(lib_dir):
    try:
        with open(config_path(lib_dir), encoding="utf-8") as f:
            conf = json.load(f)
    except Exception:
        conf = {}
    for key, value in DEFAULTS.items():
        conf.setdefault(key, value.copy() if isinstance(value, (dict, list)) else value)
    return conf


def save_conf(lib_dir, incoming):
    conf = load_conf(lib_dir)
    for key, value in incoming.items():
        if key == "apiKey" and not value:
            continue          # Empty means "leave it alone", not "erase the stored key".
        conf[key] = value
    os.makedirs(lib_dir, exist_ok=True)
    tmp = config_path(lib_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path(lib_dir))
    try:
        os.chmod(config_path(lib_dir), 0o600)   # The key lives here; keep it to this account.
    except OSError:
        pass
    return conf


def get_key(lib_dir):
    return ((load_conf(lib_dir).get("apiKey") or "").strip()
            or (os.environ.get("TYPESAFE_API_KEY") or "").strip())


def looks_secret(text, folder="", conf=None):
    """Return (is_sensitive, reason), checking folders, own terms, then rules."""
    c = conf or {}
    if folder and folder in set(c.get("neverSend", DEFAULT_SECRET_FOLDERS)):
        return True, f'Folder "{folder}" is on the never-send list'
    t = (text or "").lower()
    for word in c.get("customSecrets", []):
        w = (word or "").strip()
        if w and w.lower() in t:
            return True, f'Contains your own term "{w}"'
    if c.get("useBuiltinSecrets", True):
        for label, pattern in BUILTIN_SECRETS:
            if pattern.search(text or ""):
                return True, label
    return False, ""


def keyword_hit(text, keyword_map):
    """Decide locally when a keyword matches exactly one project.

    Matching several projects is deliberately not a decision: a note naming two
    of your projects is precisely the kind a person should look at."""
    lowered = (text or "").lower()
    hits = [project for project, words in keyword_map.items()
            if any(w.strip() and w.strip().lower() in lowered for w in words)]
    return hits[0] if len(hits) == 1 else None


def _qid(index):
    """Project names may contain spaces and non-ASCII text, so they are not used
    as question keys directly; the index maps the answer back."""
    return f"c{index}"


def build_questions(projects, language="en"):
    """projects: [(name, description)]. A missing description still works but
    measurably lowers accuracy — the model then has only the name to go on."""
    questions = {}
    for index, (name, description) in enumerate(projects):
        detail = (description or "").strip()
        if language == "zh":
            instructions = f"这条备忘录是否属于「{name}」这一类？"
            if detail:
                instructions += f"这一类装的是：{detail}"
            criteria = {
                "true": f"内容确实属于「{name}」",
                "false": f"内容不属于「{name}」，或者只是顺带提到，并非这条笔记的主题",
            }
        else:
            instructions = f"Does this note belong in the project “{name}”?"
            if detail:
                instructions += f" That project holds: {detail}"
            criteria = {
                "true": f"The note's content belongs in “{name}”.",
                "false": (f"The note does not belong in “{name}”, or only mentions it "
                          f"in passing rather than being about it."),
            }
        questions[_qid(index)] = {"type": "noul", "instructions": instructions,
                                  "criteria": criteria}
    return questions


def call_jev(key, state, questions, timeout=60):
    body = json.dumps({"state": state, "model": MODEL, "questions": questions},
                      ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_scores(response, projects):
    """Flatten the reply to {project: probability}.

    The SDK exposes response.nouls[id].noul; the HTTP shape has varied, so the
    known spellings are all accepted rather than silently scoring zero."""
    box = response.get("nouls") or response.get("answers") or response
    scores = {}
    for index, (name, _) in enumerate(projects):
        value = box.get(_qid(index)) if isinstance(box, dict) else None
        if isinstance(value, dict):
            value = value.get("noul", value.get("value", value.get("probability")))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            scores[name] = float(value)
    return scores


def judge(scores, conf):
    """Turn a set of probabilities into one conclusion."""
    if not scores:
        return {"verdict": VERDICT_ERROR, "project": "", "score": 0.0,
                "runnerUp": "", "runnerUpScore": 0.0}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_score = ranked[0]
    second, second_score = ranked[1] if len(ranked) > 1 else ("", 0.0)
    if top_score < conf["doubtThreshold"]:
        verdict = VERDICT_UNFILED           # Looks like none of them.
    elif top_score >= conf["autoThreshold"] and (top_score - second_score) >= conf["minGap"]:
        verdict = VERDICT_AUTO              # High enough, and nothing close behind.
    else:
        verdict = VERDICT_CONFIRM           # Too low, or two projects too close together.
    return {"verdict": verdict, "project": top, "score": round(top_score, 3),
            "runnerUp": second, "runnerUpScore": round(second_score, 3)}


def classify(lib_dir, items, projects, fetch_bodies, language="en"):
    """items: [{'id', 'name', 'folder'}]; projects: [(name, description)].

    fetch_bodies takes a list of note IDs and returns (bodies, error), reusing
    the server's existing Notes access. Returns (rows, error)."""
    if not projects:
        return None, "Create at least one project before asking Jev to sort notes into them."
    if len(items) > MAX_ITEMS:
        return None, f"Classify at most {MAX_ITEMS} notes at a time."

    conf = load_conf(lib_dir)
    key = get_key(lib_dir)
    questions = build_questions(projects, language)

    excluded = set(conf.get("neverSend", DEFAULT_SECRET_FOLDERS))
    readable = [item for item in items if not (item.get("folder") and item["folder"] in excluded)]
    bodies, error = fetch_bodies([item["id"] for item in readable]) if readable else ([], None)
    if error:
        return None, error
    texts = {item["id"]: (bodies[i] if i < len(bodies) else "")
             for i, item in enumerate(readable)}

    def one(item):
        body = texts.get(item["id"]) or ""
        title = item.get("name", "")
        # The body goes back to your own browser so you can check the call
        # against the text it was made on. It does not leave this Mac.
        row = {"id": item["id"], "title": title,
               "body": body[:PREVIEW_LIMIT], "bodyLength": len(body),
               "bodyTruncated": len(body) > PREVIEW_LIMIT}

        # A folder exclusion is checked before body access and holds even when
        # sending sensitive content is enabled. Turning built-ins off leaves
        # custom terms and folder exclusions in force.
        folder = item.get("folder", "")
        secret, why = looks_secret(f"{body} {title}", folder, conf)
        if secret and ((folder and folder in excluded) or not conf.get("sendSensitive")):
            reason = translate(why, language)
            stopped = translate("Stopped on this Mac, not sent.", language)
            row.update({"verdict": VERDICT_SKIPPED, "project": "", "score": 0.0,
                        "runnerUp": "", "runnerUpScore": 0.0,
                        "error": f"{reason}. {stopped}"})
            return row

        # Keyword layer first: if the note names the project outright, there is
        # nothing to guess and nothing to pay for.
        hit = keyword_hit(f"{title}\n{body[:600]}", conf.get("keywords", {}))
        if hit:
            row.update({"verdict": VERDICT_AUTO, "project": hit, "score": 1.0,
                        "runnerUp": "", "runnerUpScore": 0.0,
                        "basis": "Keyword matched in the title or opening"})
            return row

        if not key:
            row.update({"verdict": VERDICT_NO_KEY, "project": "", "score": 0.0,
                        "runnerUp": "", "runnerUpScore": 0.0,
                        "error": "No keyword matched. Add an API key in Settings to use Jev."})
            return row

        state = {"title": title, "body": body[:BODY_LIMIT]}
        try:
            scores = extract_scores(call_jev(key, state, questions), projects)
            row.update(judge(scores, conf))
            row["scores"] = {k: round(v, 3) for k, v in
                             sorted(scores.items(), key=lambda kv: -kv[1])}
            # How many projects were asked about, and how many came back with a
            # score. A mismatch means the response field names do not match what
            # is read above, which makes every score look inexplicably low. That
            # has to stay visible: it is a wiring fault, not a bad judgement.
            row["asked"] = len(projects)
            row["answered"] = len(scores)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            row.update({"verdict": VERDICT_ERROR, "project": "",
                        "error": f"HTTP {e.code} {detail}".strip()})
        except Exception as e:
            row.update({"verdict": VERDICT_ERROR, "project": "", "error": str(e)})
        return row

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        rows = list(pool.map(one, items))
    return rows, None
