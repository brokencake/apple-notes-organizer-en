#!/usr/bin/env python3
# Local Notes server: serve the UI, access Notes, and maintain text mirrors.
import copy
import gzip
import threading
import time
import uuid
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import quote, unquote, urlparse, parse_qs
import jev_classify
import similar_groups
from locale_support import (REQUEST_LANGUAGE,
                            request_language, system_locale, translate)

ROOT = os.path.dirname(os.path.abspath(__file__))

# This edition names everything it writes to disk in English, so the folder a
# user opens in Finder reads the same way as the interface. Earlier editions
# wrote Chinese names; those are migrated at startup, newest legacy name first,
# and the contents are never rewritten. See migrate_legacy_names().
LIB_DIR = os.path.join(ROOT, "Notes Library")
LEGACY_LIB_DIRS = [os.path.join(ROOT, name) for name in ("备忘录整理", "prompt库")]
DATA_FILE = os.path.join(LIB_DIR, "library.json")
BACKUP_FILE = os.path.join(LIB_DIR, "library.backup.json")
MIRROR_DIR = os.path.join(LIB_DIR, "Text Mirror")
SNAP_DIR = os.path.join(LIB_DIR, "Snapshots")
HISTORY_DIR = os.path.join(LIB_DIR, "Save History")
HISTORY_RECENT_LIMIT = 10
HISTORY_HOURLY_LIMIT = 12
WRITE_LOCK = threading.RLock()
RESTORE_LOCK = threading.Lock()

# The browser pings every three seconds. Stop an orphaned server after the last
# window closes, while allowing a slower first browser launch or a quick reload.
PING_GRACE = 12
FIRST_PING_GRACE = 90
_last_ping = None
_ping_lock = threading.Lock()


def touch_ping():
    global _last_ping
    with _ping_lock:
        _last_ping = time.monotonic()


def ping_expired(started, current, last):
    return current - (started if last is None else last) > (
        FIRST_PING_GRACE if last is None else PING_GRACE)


def watchdog():
    started = time.monotonic()
    while True:
        time.sleep(2)
        with _ping_lock:
            last = _last_ping
        if ping_expired(started, time.monotonic(), last):
            try:
                write_session_log()
            except (OSError, ValueError):
                pass
            os._exit(0)

# Legacy name -> current name, for files and folders inside the library.
LEGACY_ENTRIES = [("库.json", "library.json"),
                  ("库.backup.json", "library.backup.json"),
                  ("txt镜像", "Text Mirror"),
                  ("快照", "Snapshots"),
                  ("归位表.json", "restore-map.json"),
                  ("移动记录", "Move Logs")]

ACTIVE_TAG = "_active"          # Marks the active version in mirror filenames.
LEGACY_ACTIVE_TAG = "_现役"
UNFILED_DIR = "Unfiled"         # Mirror folder for entries with no project.
LEGACY_UNFILED_DIR = "未分类"
UNTITLED = "Untitled"
PORT = int(os.environ.get("PORT", 8788))   # Override an occupied port: PORT=8799 ./note-organizer.command
URL = f"http://localhost:{PORT}/" + quote("app.html")
SESSION_TOKEN = secrets.token_urlsafe(32)
STATIC_PATHS = frozenset(("/app.html", "/app.en.html", "/app.zh-CN.html", "/language.js"))

US = "\x1f"  # Field separator
RS = "\x1e"  # Record separator

LIST_SCRIPT = '''
on ownerOfFolder(f)
 tell application "Notes"
  set node to f
  repeat 64 times
   if class of node is account then return node
   if class of node is not folder then error "The folder account could not be verified."
   set node to container of node
  end repeat
 end tell
 error "The folder hierarchy could not be resolved."
end ownerOfFolder
set us to character id 31
set rs to character id 30
set out to {}
tell application "Notes"
 set ac to count of accounts
 repeat with f in every folder
  set fn to name of f
  if fn is not in {"Recently Deleted", "最近删除", "最近刪除"} then
   set a to my ownerOfFolder(f)
   set aid to id of a as text
   set aname to name of a as text
   set fid to id of f as text
   set nNames to name of every note of f
   set nIds to id of every note of f
   set nDates to modification date of every note of f
   repeat with i from 1 to count of nIds
    set d to item i of nDates
    set ds to (year of d as text) & "-" & (month of d as integer as text) & "-" & (day of d as text) & "-" & (hours of d as text) & "-" & (minutes of d as text)
    set end of out to fn & us & (item i of nNames) & us & (item i of nIds) & us & ds & us & aid & us & aname & us & fid & us & (ac as text)
   end repeat
  end if
 end repeat
end tell
set AppleScript's text item delimiters to rs
return out as text
'''

BODY_SCRIPT = '''
on run argv
    set rs to character id 30
    set us to character id 31
    set out to {}
    tell application id "com.apple.Notes"
        repeat with nid in argv
            try
                set bodyText to plaintext of note id nid
                set end of out to "1" & us & bodyText
            on error errMsg
                set end of out to "0" & us & errMsg
            end try
        end repeat
    end tell
    set AppleScript's text item delimiters to rs
    return (out as text) & rs
end run
'''

FOLDERS_SCRIPT = '''
set us to character id 31
set rs to character id 30
set defaultName to ""
set defaultId to ""
tell application id "com.apple.Notes"
    set fs to name of every folder
    try
        set defaultTarget to default folder of default account
        set defaultName to name of defaultTarget
        set defaultId to id of defaultTarget
    end try
end tell
set AppleScript's text item delimiters to us
return (fs as text) & rs & defaultName & us & defaultId
'''

FOLDER_DETAILS_SCRIPT = '''
on ownerOfFolder(f)
 tell application id "com.apple.Notes"
  set node to f
  repeat 64 times
   if class of node is account then return node
   if class of node is not folder then error "The folder account could not be verified."
   set node to container of node
  end repeat
 end tell
 error "The folder hierarchy could not be resolved."
end ownerOfFolder
set us to character id 31
set rs to character id 30
set out to {}
tell application id "com.apple.Notes"
 repeat with f in every folder
  set fn to name of f as text
  if fn is not in {"Recently Deleted", "最近删除", "最近刪除"} then
   set a to my ownerOfFolder(f)
   set end of out to fn & us & (id of f as text) & us & (id of a as text) & us & (name of a as text)
  end if
 end repeat
end tell
set AppleScript's text item delimiters to rs
return out as text
'''

# Update the specified note, or create one in the selected folder. Never delete notes.
PUSH_SCRIPT = '''
on run argv
    set nid to item 1 of argv
    set fname to item 2 of argv
    set ttl to item 3 of argv
    set bd to item 4 of argv
    tell application id "com.apple.Notes"
        if nid is not "" then
            set n to note id nid
            set body of n to bd
            return id of n
        end if
        set tgt to missing value
        repeat with f in every folder
            if name of f is fname then
                set tgt to f
                exit repeat
            end if
        end repeat
        if tgt is missing value then
            set tgt to make new folder with properties {name:fname}
        end if
        set n to make new note at tgt with properties {name:ttl, body:bd}
        return id of n
    end tell
end run
'''


RESTORE_FILE = os.path.join(LIB_DIR, "restore-map.json")

# Move notes into project folders, creating folders as needed. Do not create or delete notes.
MOVE_SCRIPT = '''
-- Account checks live in the shared algorithm; only these adapters send Notes events.
on objectId(obj)
 tell application "Notes" to return id of obj as text
end objectId
on noteById(nid)
 tell application "Notes" to return note id nid
end noteById
on ownerOfFolder(f)
 tell application "Notes"
  set node to f
  repeat 64 times
   if class of node is account then return node
   if class of node is not folder then error "The folder account could not be verified. No move was made." number 1701
   set node to container of node
  end repeat
 end tell
 error "The folder hierarchy could not be resolved. No move was made." number 1701
end ownerOfFolder
on accountForNote(n)
 tell application "Notes" to set f to container of n
 return my ownerOfFolder(f)
end accountForNote
on folderOwner(f)
 return my ownerOfFolder(f)
end folderOwner
on foldersNamed(a, tgt)
 set found to {}
 set aid to my objectId(a)
 tell application "Notes"
  repeat with f in every folder
   if name of f is tgt then
    if my objectId(my ownerOfFolder(f)) is aid then set end of found to contents of f
   end if
  end repeat
 end tell
 return found
end foldersNamed
on folderById(fid)
 tell application "Notes"
  if exists folder id fid then return folder id fid
 end tell
 return missing value
end folderById
on createFolder(a, tgt)
 tell application "Notes" to return make new folder at a with properties {name:tgt}
end createFolder
on moveOne(n, dst)
 tell application "Notes" to move n to dst
end moveOne

-- SHARED ALGORITHM: the fixture replaces only the event adapters above.
on run argv
 if (count of argv) < 4 then error "Move parameters are missing an account boundary." number 1701
 set tgt to item 1 of argv
 set expectedAccount to item 2 of argv
 set preferredFolder to item 3 of argv
 set okN to 0
 set badN to 0
 set details to {}
 set cacheKeys to {}
 set cacheTargets to {}
 set us to character id 31
 set rs to character id 30
 repeat with i from 4 to (count of argv)
  set nid to item i of argv
  try
   set n to my noteById(nid)
   set a to my accountForNote(n)
   set aid to my objectId(a)
   if aid is "" then error "The source account could not be verified. No move was made." number 1701
   if expectedAccount is not "" and aid is not expectedAccount then error "The note account changed. A cross-account move was blocked; reopen the preview." number 1701
   set cacheKey to {aid, tgt, preferredFolder}
   set dst to missing value
   repeat with ci from 1 to count of cacheKeys
    if item ci of cacheKeys is cacheKey then
     set dst to item ci of cacheTargets
     exit repeat
    end if
   end repeat
   if dst is missing value and preferredFolder is not "" then
    set dst to my folderById(preferredFolder)
    if dst is missing value then error "The selected destination folder no longer exists. Reopen the preview." number 1701
    if my objectId(my folderOwner(dst)) is not aid then error "The selected destination folder belongs to another account. No move was made." number 1701
    tell application id "com.apple.Notes" to set actualName to name of dst
    if actualName is not tgt then error "The selected destination folder was renamed. Reopen the preview." number 1701
   end if
   if dst is missing value then
    set matches to my foldersNamed(a, tgt)
    if (count of matches) > 1 then error "This account has multiple folders with the same name. Rename them before retrying; no move was made." number 1701
    if (count of matches) is 1 then
     set dst to item 1 of matches
    else
     set dst to my createFolder(a, tgt)
    end if
   end if
   if cacheKey is not in cacheKeys then
    set end of cacheKeys to cacheKey
    set end of cacheTargets to dst
   end if
   -- Recheck both sides just before the move; a stale preview never authorizes a cross-account move.
   if my objectId(my accountForNote(n)) is not aid then error "The source account changed during execution. No move was made." number 1701
   if my objectId(my folderOwner(dst)) is not aid then error "The destination account could not be verified. No move was made." number 1701
   set dstId to my objectId(dst)
   my moveOne(n, dst)
   set okN to okN + 1
   set end of details to "M" & us & nid & us & dstId
  on error msg
   set badN to badN + 1
   -- Fixed separators must never be introduced by a free-form error string.
   set AppleScript's text item delimiters to {us, rs, return, linefeed}
   set pieces to text items of msg
   set AppleScript's text item delimiters to " "
   set cleanMessage to pieces as text
   set end of details to "F" & us & nid & us & cleanMessage
  end try
 end repeat
 set AppleScript's text item delimiters to rs
 if (count of details) is 0 then return (okN as text) & "/" & (badN as text)
 return (okN as text) & "/" & (badN as text) & rs & (details as text)
end run
'''


def list_notes():
    out, err = run_osascript(LIST_SCRIPT)
    if err:
        return None, err
    try:
        return parse_notes(out), None
    except ValueError as exc:
        return None, str(exc)


def list_folder_details():
    out, err = run_osascript(FOLDER_DETAILS_SCRIPT)
    if err:
        return None, err
    folders, seen = [], set()
    for row in (out or "").split(RS):
        if not row:
            continue
        parts = row.split(US)
        if len(parts) != 4 or not all(parts[:3]) or parts[1] in seen:
            return None, "The folder list is incomplete or contains duplicate folder IDs."
        seen.add(parts[1])
        folders.append(dict(name=parts[0], id=parts[1], accountId=parts[2], accountName=parts[3]))
    return folders, None


def load_restore_book():
    try:
        with open(RESTORE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"说明": translate('Original note locations recorded before moving, for undo'), "记录": []}


def save_restore_book(book):
    with WRITE_LOCK:
        os.makedirs(LIB_DIR, exist_ok=True)
        tmp = RESTORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(book, f, ensure_ascii=False, indent=1)
        os.replace(tmp, RESTORE_FILE)


MOVELOG_DIR = os.path.join(LIB_DIR, "Move Logs")
SESSION_STATE = None


def start_session():
    """Capture the library at process start; page refreshes do not reset it."""
    global SESSION_STATE
    with WRITE_LOCK:
        SESSION_STATE = {"started": datetime.now(), "baseline": copy.deepcopy(load_data()),
                         "moves": [], "sync": [], "language": system_locale()["language"],
                         "id": uuid.uuid4().hex[:8], "path": None}
        write_session_log()


def session_changes(baseline, current, moves):
    before = {p.get("id"): p for p in baseline.get("prompts", [])}
    after = {p.get("id"): p for p in current.get("prompts", [])}
    groups = {"created": [], "body": [], "category": [], "deleted": [], "moved": copy.deepcopy(moves)}
    for ident, p in after.items():
        row = {"id": ident, "title": p.get("title") or "Untitled", "time": p.get("updated") or ""}
        old = before.get(ident)
        if old is None:
            groups["created"].append(row)
            continue
        old_bodies = [(v.get("v"), v.get("content")) for v in old.get("versions", [])]
        new_bodies = [(v.get("v"), v.get("content")) for v in p.get("versions", [])]
        if old_bodies != new_bodies or old.get("title") != p.get("title"):
            groups["body"].append(row)
        if (old.get("project") or "") != (p.get("project") or ""):
            groups["category"].append({**row, "from": old.get("project") or "", "to": p.get("project") or ""})
    for ident, p in before.items():
        if ident not in after:
            groups["deleted"].append({"id": ident, "title": p.get("title") or "Untitled", "time": ""})
    return groups


def session_report():
    with WRITE_LOCK:
        if SESSION_STATE is None:
            return {"error": "No active session record is available."}
        return {"started": SESSION_STATE["started"].isoformat(timespec="seconds"),
                "ended": datetime.now().isoformat(timespec="seconds"),
                "path": SESSION_STATE["path"],
                "groups": session_changes(SESSION_STATE["baseline"], load_data(), SESSION_STATE["moves"]),
                "sync": copy.deepcopy(SESSION_STATE["sync"])}


def write_session_log(language=None):
    """Replace the current local TXT after every committed change or move."""
    with WRITE_LOCK:
        state = SESSION_STATE
        if state is None:
            return None
        if language in ("en", "zh"):
            state["language"] = language
        started, ended = state["started"], datetime.now()
        report = session_changes(state["baseline"], load_data(), state["moves"])
        zh = state["language"] == "zh"
        labels = ({"created": "新建", "body": "正文或标题修改", "category": "分类修改", "deleted": "删除", "moved": "移动"}
                  if zh else {"created": "Created", "body": "Body or title edited", "category": "Category changed", "deleted": "Deleted", "moved": "Moved"})
        lines = [("使用记录" if zh else "Session log"),
                 ("开始：" if zh else "Started: ") + started.strftime("%Y-%m-%d %H:%M:%S"),
                 ("结束：" if zh else "Ended: ") + ended.strftime("%Y-%m-%d %H:%M:%S"), ""]
        for key, label in labels.items():
            rows = report[key]
            lines.append(f"{label} ({len(rows)})")
            for row in rows:
                title = str(row.get("title") or "Untitled").replace("\n", " ").replace("\r", " ")
                route = f" · {row.get('from') or '?'} → {row.get('to') or '?'}" if key in ("category", "moved") else ""
                lines.append(f"- {title}{route} · {row.get('time') or ''}")
            lines.append("")
        lines.append("同步结果" if zh else "Sync results")
        for result in state["sync"]:
            lines.append(f"- {result['time']} · {result['label']} · {result['moved']} / {result['failed']}")
        os.makedirs(MOVELOG_DIR, exist_ok=True)
        end_tag = ended.strftime("%H%M") if ended.date() == started.date() else ended.strftime("%Y-%m-%d_%H%M")
        path = os.path.join(MOVELOG_DIR, f"session-log_{started:%Y-%m-%d_%H%M}-{end_tag}_{state['id']}.txt")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            if state["path"] and state["path"] != path and os.path.exists(state["path"]):
                os.remove(state["path"])
            state["path"] = path
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return path


def record_session_moves(entries, destinations, label, failed=0):
    if SESSION_STATE is None:
        return
    with WRITE_LOCK:
        timestamp = datetime.now().isoformat(timespec="seconds")
        for entry in entries:
            nid = entry["id"]
            if nid in destinations:
                SESSION_STATE["moves"].append({"noteId": nid, "title": entry["笔记标题"],
                    "from": entry["原文件夹"], "to": entry["目标文件夹"],
                    "accountId": entry["原账户ID"], "folderId": destinations[nid], "time": timestamp})
        SESSION_STATE["sync"].append({"time": timestamp, "label": label,
                                       "moved": len(destinations), "failed": failed})
        write_session_log(REQUEST_LANGUAGE.get())


def md_cell(s):
    """Keep pipes and newlines in titles from breaking Markdown tables."""
    return str(s or "").replace("|", "｜").replace("\n", " ").replace("\r", " ").strip()


def write_move_log(title, entries, note="", key=None):
    """Write a local Markdown audit log for each move.
    Entries keep the legacy note-title/from/to keys. Calls sharing a key append
    to one file, so a multi-batch operation has one log. Logging failures must
    not interrupt the move itself."""
    try:
        os.makedirs(MOVELOG_DIR, exist_ok=True)
        now = datetime.now()
        stamp = key or f"{now:%Y-%m-%d_%H%M%S}"
        path = os.path.join(MOVELOG_DIR, f"move-log_{stamp}.md")
        fresh = not os.path.exists(path)
        rows = sorted(entries, key=lambda e: (e.get("原文件夹", ""),
                                              e.get("目标文件夹", ""),
                                              e.get("笔记标题", "")))
        with open(path, "a", encoding="utf-8") as f:
            if fresh:
                f.write(translate(f"# Notes movement log · {now:%Y-%m-%d %H:%M}") + "\n\n")
                f.write(translate("> Only folders changed. Note content was not edited, and no notes were deleted.") + "\n")
                f.write(translate("> To undo: Organizer → Sync back to phone → Undo (repeat for earlier batches).") + "\n\n")
            f.write(f"## {title}\n\n")
            f.write(translate(f"**{len(rows)}** notes in total."))
            if note:
                f.write(note)
            f.write("\n\n" + translate("| Note | From | To |") + "\n| --- | --- | --- |\n")
            for e in rows:
                f.write(f"| {md_cell(e.get('笔记标题'))} "
                        f"| {md_cell(e.get('原文件夹') or '?')} "
                        f"| {md_cell(e.get('目标文件夹') or '?')} |\n")
            f.write("\n")
        return path
    except Exception:
        return None


def move_notes(moves, label, log_key=None):
    """Resolve each destination inside the source account; journal before any move."""
    notes, err = list_notes()
    if err:
        return None, err
    folder_by_id = {}
    if any(m.get("folderId") for m in moves):
        details, err = list_folder_details()
        if err:
            return None, err
        folder_by_id = {f["id"]: f for f in details}
    by_id = {n["id"]: n for n in notes}
    entries, groups, failures, seen = [], {}, [], set()
    for m in moves:
        nid, folder = m.get("noteId"), (m.get("folder") or "").strip()
        folder_id = m.get("folderId") or ""
        n = by_id.get(nid)
        if not n or not folder or nid in seen:
            continue
        seen.add(nid)
        account = n.get("accountId")
        reason = None
        if not account or not n.get("folderId"):
            reason = translate('The note account could not be verified. No move was made; reopen the preview.')
        elif "accountId" in m and m["accountId"] != account:
            reason = translate('The account differs from the preview. No move was made; reopen the preview.')
        elif folder_id and (folder_by_id.get(folder_id, {}).get("accountId") != account
                            or folder_by_id.get(folder_id, {}).get("name") != folder):
            reason = translate('The selected destination folder changed or belongs to another account. No move was made.')
        if reason:
            failures.append({"id": nid, "title": n["name"], "reason": reason})
            continue
        if (folder_id and n["folderId"] == folder_id) or (not folder_id and n["folder"] == folder):
            continue
        entries.append({"id": nid, "笔记标题": n["name"],
                        "原文件夹": n["folder"], "目标文件夹": folder,
                        "原账户ID": account, "原账户": n["accountName"],
                        "目标账户ID": account, "目标账户": n["accountName"],
                        "原文件夹ID": n["folderId"]})
        groups.setdefault((account, folder, folder_id), []).append(nid)
    if not entries:
        return {"moved": 0, "failed": len(failures), "skipped": len(moves) - len(failures),
                "failures": failures}, None
    book = load_restore_book()
    book["记录"].append({"时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "说明": label or "", "笔记": entries})
    book["记录"] = book["记录"][-50:]
    save_restore_book(book)
    moved = 0
    confirmed = {}
    def partial_error(reason):
        if confirmed:
            rows = [e for e in entries if e["id"] in confirmed]
            write_move_log(label or translate('Sync back to phone'), rows, key=log_key)
            try:
                record_session_moves(rows, confirmed, label or translate('Sync back to phone'), len(failures))
            except (OSError, ValueError) as exc:
                reason += f"; session log update failed: {exc}"
        return None, reason
    for (account, folder, folder_id), ids in groups.items():
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            out, err = run_osascript(MOVE_SCRIPT, [folder, account, folder_id] + batch,
                                     timeout=300, verb="organize")
            if err:
                return partial_error(err)
            try:
                done, bad = parse_move_result(out, batch)
            except ValueError as exc:
                return partial_error(translate(f'{exc}; the restore record was retained. Check the current location.'))
            confirmed.update(done)
            moved += len(done)
            failures.extend({"id": nid, "title": by_id[nid]["name"], "reason": reason}
                            for nid, reason in bad.items())
    confirmed_rows = [e for e in entries if e["id"] in confirmed]
    log = write_move_log(label or translate('Sync back to phone'), confirmed_rows, key=log_key) if confirmed_rows else None
    log_warning = ""
    try:
        record_session_moves(confirmed_rows, confirmed, label or translate('Sync back to phone'), len(failures))
    except (OSError, ValueError) as exc:
        log_warning = str(exc)
    return {"moved": moved, "failed": len(failures),
            "skipped": len(moves) - len(entries) - sum(e["id"] not in {n["id"] for n in entries} for e in failures),
            "failures": failures, "log": log, "sessionWarning": log_warning}, None


def norm_date(raw):
    """Pad AppleScript year-month-day-hour-minute values as YYYY-MM-DD hh:mm.
    Preserve this format so the frontend can sort dates as strings."""
    try:
        y, m, d, h, mi = [int(x) for x in raw.split("-")]
        return f"{y:04d}-{m:02d}-{d:02d} {h:02d}:{mi:02d}"
    except Exception:
        return raw


def note_html(title, text):
    """Convert plain text to Notes HTML. Notes uses the first line as its title."""
    def e(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = ["<div><b>" + e(title) + "</b></div>"]
    for ln in lines:
        out.append("<div>" + (e(ln) if ln.strip() else "<br>") + "</div>")
    return "".join(out)


def run_osascript(script, args=None, timeout=180, verb="read"):
    cmd = ["osascript", "-e", script]
    if args:
        cmd += list(args)
    try:
        # osascript emits UTF-8; decoding must not depend on the shell's locale.
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"The request to {verb} Apple Notes timed out. Please try again."
    if r.returncode != 0:
        err = r.stderr.strip()
        if "-1743" in err or "1743" in err:
            return None, (f"Permission to {verb} Apple Notes was denied. Open System Settings → Privacy & Security → Automation, "
                          "allow Terminal to control Notes, then try again.")
        return None, f"Could not {verb} Apple Notes: {err}"
    return r.stdout.rstrip("\n"), None


def parse_body_results(out, ids):
    """Retain per-note read failures; an empty successful body is still valid."""
    if not isinstance(out, str) or not out.endswith(RS):
        raise ValueError("Apple Notes returned an incomplete body response.")
    records = out[:-1].split(RS) if ids else []
    if len(records) != len(ids):
        raise ValueError("Apple Notes returned the wrong number of note bodies.")
    results = []
    for nid, record in zip(ids, records):
        state, separator, value = record.partition(US)
        if not separator or state not in ("0", "1"):
            raise ValueError("Apple Notes returned an invalid body status.")
        results.append({"id": nid, "ok": True, "body": value} if state == "1"
                       else {"id": nid, "ok": False, "error": value or "The note could not be read."})
    return results


def load_data():
    # 旧库仅在返回值补 rev=0；读取和升级本身不写入用户数据。
    data = None
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            if os.path.exists(BACKUP_FILE):
                with open(BACKUP_FILE, encoding="utf-8") as f:
                    data = json.load(f)
            else:
                raise
    if data is None:
        data = {"projects": [], "prompts": [], "importedNoteIds": []}
    data.setdefault("rev", 0)
    if type(data["rev"]) is not int or data["rev"] < 0:
        raise ValueError(translate('The library revision is invalid. Check the library file before saving.'))
    return data


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" ._")
    return name[:80] or UNTITLED


def mirror_ext(data):
    """Markdown mirrors contain the exact same plain text, with a useful suffix."""
    return ".md" if (data or {}).get("mirrorExt") == ".md" else ".txt"


def mirror_plan(data):
    """Map (entry ID, version number) to a relative mirror path.
    Share this calculation between mirror generation and file reveal; separate
    calculations could reveal the wrong entry when filenames collide."""
    ext = mirror_ext(data)
    plan, used = {}, set()
    for p in data.get("prompts", []):
        proj = safe_name(p.get("project") or UNFILED_DIR)
        title = safe_name(p.get("title", UNTITLED))
        active = p.get("active")
        for v in p.get("versions", []):
            tag = ACTIVE_TAG if v.get("v") == active else ""
            rel = f"{proj}/{title}_v{v.get('v')}{tag}{ext}"
            n = 2
            while rel in used:
                rel = f"{proj}/{title}_{n}_v{v.get('v')}{tag}{ext}"
                n += 1
            used.add(rel)
            plan[(p.get("id"), v.get("v"))] = rel
    return plan


def rebuild_mirror(data):
    """Synchronize by content: calculate all expected files, write changed
    content, then remove obsolete files. Do not rebuild thousands of files
    from scratch on every save."""
    plan = mirror_plan(data)
    wanted = {}  # Relative path -> content
    ext = mirror_ext(data)
    wanted["README" + ext] = (translate("This folder is generated by Apple Notes Organizer and synchronized on every save.") + "\n" +
                            translate("Edit content in the organizer browser interface. Direct edits to these {ext} files are overwritten on the next save.").format(ext=ext) + "\n")
    for p in data.get("prompts", []):
        for v in p.get("versions", []):
            rel = plan.get((p.get("id"), v.get("v")))
            if rel:
                wanted[rel] = v.get("content", "")

    os.makedirs(MIRROR_DIR, exist_ok=True)
    for rel, content in wanted.items():
        path = os.path.join(MIRROR_DIR, rel)
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == content:
                    continue
        except Exception:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    # Remove obsolete files and empty folders.
    for dirpath, _, filenames in os.walk(MIRROR_DIR, topdown=False):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if os.path.relpath(full, MIRROR_DIR) not in wanted:
                try:
                    os.remove(full)
                except OSError:
                    pass
        if dirpath != MIRROR_DIR and not os.listdir(dirpath):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


def project_dir(project):
    """Return the project mirror folder. Keep naming consistent with rebuild_mirror."""
    return os.path.join(MIRROR_DIR, safe_name(project or UNFILED_DIR))


def snapshot_if_shrinking(new_data):
    """Save a timestamped snapshot before the entry count drops significantly.
    The one-save backup cannot protect against consecutive saves after deletion."""
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        return
    o, n = len(old.get("prompts", [])), len(new_data.get("prompts", []))
    if o - n < 10:      # Small everyday deletions do not trigger a snapshot.
        return
    os.makedirs(SNAP_DIR, exist_ok=True)
    name = f"library_{datetime.now():%Y%m%d-%H%M%S}_{o}-entries.json"
    try:
        with open(os.path.join(SNAP_DIR, name), "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=1)
        for s in sorted(os.listdir(SNAP_DIR))[:-10]:   # Keep only the 10 most recent snapshots.
            os.remove(os.path.join(SNAP_DIR, s))
    except OSError:
        pass


def save_data(data):
    with WRITE_LOCK:
        # 任何 mkdir / 备份 / 快照 / 镜像都必须在 CAS 校验通过之后。
        current = check_revision(data.get("rev"))
        committed = dict(data, rev=current + 1)
        os.makedirs(LIB_DIR, exist_ok=True)
        preserve_history(current)
        snapshot_if_shrinking(committed)
        if os.path.exists(DATA_FILE):
            shutil.copyfile(DATA_FILE, BACKUP_FILE)
        atomic_json(DATA_FILE, committed)
        result = {"ok": True, "rev": committed["rev"]}
        try:
            rebuild_mirror(committed)
        except Exception as e:
            # 库已提交，不可返回500让客户端继续拿旧rev重试。
            result["warning"] = translate(f'The library was saved, but the text mirror could not be updated: {e}')
        try:
            write_session_log(REQUEST_LANGUAGE.get())
        except (OSError, ValueError) as e:
            warning = translate(f'The library was saved, but the session log could not be updated: {e}')
            result["warning"] = (result.get("warning", "") + "\n" + warning).strip()
        return result


def save_similar_change(operation, groups=None):
    """Keep the previous undo record if saving fails before the library commits.

    The ported merge algorithm manages its own log. Coordinate it with the
    existing atomic library save here, without changing grouping semantics.
    A mirror failure after the library commits must retain the new log state.
    """
    original = load_data()
    path = similar_groups.record_path(LIB_DIR)
    try:
        with open(path, "rb") as f:
            previous_record = f.read()
    except FileNotFoundError:
        previous_record = None
    try:
        if operation == "merge":
            data, stat = similar_groups.merge(copy.deepcopy(original), groups, LIB_DIR)
        else:
            data, stat = similar_groups.undo(copy.deepcopy(original), LIB_DIR)
        if not stat.get("error") and stat.get("groups"):
            stat.update(save_data(data))
        return stat
    except Exception:
        if load_data() == original:
            if previous_record is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(previous_record)
                os.replace(tmp, path)
        raise


def parse_notes(out):
    """Account-aware protocol; legacy rows remain readable, but cannot authorize moves."""
    if not isinstance(out, str):
        raise ValueError(translate('The note location response has an invalid format.'))
    notes, seen = [], set()
    for row in (out.split(RS) if out else []):
        p = row.split(US)
        if len(p) not in (4, 8) or not p[0] or not p[2] or p[2] in seen:
            raise ValueError(translate('The note location list is malformed or contains duplicate IDs.'))
        n = {"folder": p[0], "name": p[1], "id": p[2], "date": norm_date(p[3])}
        if len(p) == 8:
            if not all(p[4:7]) or not p[7].isdigit() or int(p[7]) < 1:
                raise ValueError(translate('Note account or folder metadata is incomplete.'))
            n.update(accountId=p[4], accountName=p[5], folderId=p[6], accountCount=int(p[7]))
        notes.append(n)
        seen.add(p[2])
    return notes

def parse_move_result(out, ids):
    """Every result must account for every input ID; never trust a bare success total."""
    if not isinstance(out, str):
        raise ValueError(translate('The move response has an invalid format.'))
    rows = out.split(RS)
    counts = re.fullmatch(r"([0-9]+)/([0-9]+)", rows[0].strip())
    if not counts or sum(map(int, counts.groups())) != len(ids):
        raise ValueError(translate('Move counts could not be parsed or do not match the batch size.'))
    moved, failed, seen = {}, {}, set()
    for row in rows[1:]:
        parts = row.split(US)
        if (len(parts) != 3 or parts[0] not in ("M", "F") or parts[1] not in ids
                or parts[1] in seen or not parts[2]):
            raise ValueError(translate('An individual move receipt is invalid.'))
        seen.add(parts[1])
        (moved if parts[0] == "M" else failed)[parts[1]] = (parts[2] if parts[0] == "M" else translate(parts[2]))
    if (len(moved), len(failed)) != tuple(map(int, counts.groups())) or seen != set(ids):
        raise ValueError(translate('Individual move receipts do not match the counts.'))
    return moved, failed

def read_restore_history():
    """只读投影现有归位栈；兼容早期目标文件夹字段，不猜测缺失信息。"""
    try:
        with WRITE_LOCK:
            with open(RESTORE_FILE, encoding="utf-8") as f:
                book = json.load(f)
    except FileNotFoundError:
        book = {"记录": []}
    if not isinstance(book, dict) or not isinstance(book.get("记录"), list):
        raise ValueError(translate('The restore record format is invalid.'))
    source = book["记录"]
    records, time_counts = [], {}
    for index, rec in enumerate(source):
        if (not isinstance(rec, dict) or not isinstance(rec.get("时间"), str)
                or not isinstance(rec.get("笔记"), list)):
            raise ValueError(translate(f'Restore batch {index + 1} has an invalid timestamp or note list.'))
        for key in ("说明", "目标文件夹"):
            if key in rec and not isinstance(rec[key], str):
                raise ValueError(translate(f'Restore batch {index + 1} has an invalid description or destination folder.'))
        items = []
        for entry in rec["笔记"]:
            if not isinstance(entry, dict):
                raise ValueError(translate(f'Restore batch {index + 1} contains an invalid note detail.'))
            for key in ("笔记标题", "整理器标题", "原文件夹", "目标文件夹"):
                if key in entry and not isinstance(entry[key], str):
                    raise ValueError(translate(f'Restore batch {index + 1} contains an invalid note-detail field.'))
            items.append({"title": entry.get("笔记标题") or entry.get("整理器标题", ""),
                          "from": entry.get("原文件夹", ""),
                          "to": entry.get("目标文件夹") or rec.get("目标文件夹", "")})
        description = rec.get("说明") or (
            translate('Move to: ') + rec["目标文件夹"] if rec.get("目标文件夹") else "")
        records.append({"index": index, "time": rec["时间"],
                        "description": description, "count": len(items), "items": items})
        time_counts[rec["时间"]] = time_counts.get(rec["时间"], 0) + 1
    for rec in records:
        rec["sameTimeCount"] = time_counts[rec["time"]]
        rec["sameTimeRemaining"] = rec["sameTimeCount"] - 1
    return {"records": sorted(records, key=lambda rec: (rec["time"], rec["index"]), reverse=True), "total": len(records),
            "nextIndex": len(records) - 1 if records else None}

def restore_notes():
    """保留旧归位表结构；只有逐条复核成功的笔记才能从记录中移除。"""
    if not RESTORE_LOCK.acquire(blocking=False):
        return {"error": translate('An undo is in progress. Wait for it to finish before retrying.'), "busy": True}, 409
    try:
        with WRITE_LOCK:
            return _restore_notes_locked()
    finally:
        RESTORE_LOCK.release()

def _restore_notes_locked():
    entries, rec = [], {}

    def failure(entry, reason, current=None):
        item = {"id": entry["id"], "title": entry.get("笔记标题") or translate("Untitled"),
                "reason": reason, "originalFolder": entry["原文件夹"]}
        if current is not None:
            item["currentFolder"] = current
        return item

    def log_result(succeeded, failures):
        note = translate(f' Confirmed restored: **{len(succeeded)}**; unconfirmed: **{len(failures)}**.')
        if failures:
            note += translate(' Failed or unconfirmed restore records were retained for retry.\n\n')
            note += translate('| Unconfirmed note | id | Original folder | Reason |\n| --- | --- | --- | --- |\n')
            for e in failures:
                note += "| " + " | ".join(md_cell(e.get(k)) for k in
                                          ("title", "id", "originalFolder", "reason")) + " |\n"
        note += translate('\n\nThe table below contains only individually confirmed successes.')
        return write_move_log(
            translate(f"Undo: revert {rec.get('时间', '')} ({rec.get('说明') or translate('Move')})"),
            [{"笔记标题": e.get("笔记标题", ""),
              "原文件夹": e.get("目标文件夹", "?"),
              "目标文件夹": e["原文件夹"]} for e in succeeded], note=note)

    def abort(reason, status=500):
        failures = [failure(e, reason) for e in entries]
        log = log_result([], failures) if entries else None
        return {"error": reason, "restored": 0, "failed": len(failures),
                "failures": failures, "时间": rec.get("时间", ""),
                "recordRetained": True, "log": log}, status

    try:
        # 通用 load_restore_book 会吞掉读取错误，不能用于会删减记录的还原路径。
        try:
            with open(RESTORE_FILE, "rb") as f:
                original_bytes = f.read()
        except FileNotFoundError:
            return {"error": translate('There is no recorded move to undo.')}, 404
        book = json.loads(original_bytes.decode("utf-8"))
        if not isinstance(book, dict) or not isinstance(book.get("记录"), list):
            raise ValueError(translate('The restore record format is invalid.'))
        if not book["记录"]:
            return {"error": translate('There is no recorded move to undo.')}, 404
        rec = book["记录"][-1]
        if not isinstance(rec, dict) or not isinstance(rec.get("笔记"), list) or not rec["笔记"]:
            rec = {}
            raise ValueError(translate('The latest restore record has an invalid note list.'))
        checked, seen = [], set()
        for e in rec["笔记"]:
            if (not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"]
                    or not isinstance(e.get("原文件夹"), str) or not e["原文件夹"]
                    or e["id"] in seen):
                raise ValueError(translate('The latest restore record contains invalid or duplicate note IDs or original folders.'))
            for key in ("原账户ID", "原账户", "原文件夹ID"):
                if key in e and (not isinstance(e[key], str) or not e[key]):
                    raise ValueError(translate('The latest restore record has invalid account or folder IDs.'))
            checked.append(e)
            seen.add(e["id"])
        entries = checked
        before, err = list_notes()
        if err:
            return abort(translate(f'Accounts could not be verified before undo: {err}; the full restore record was retained.'))
        before_by_id = {n["id"]: n for n in before}
        back, unconfirmed, warnings, expected_accounts, destinations = {}, {}, [], {}, {}
        for e in entries:
            n = before_by_id.get(e["id"], {})
            account = e.get("原账户ID") or n.get("accountId")
            if not account or not n.get("accountId"):
                unconfirmed[e["id"]] = translate('The current account could not be verified. No undo was made.')
                continue
            if account != n["accountId"]:
                unconfirmed[e["id"]] = translate('The note is in another account. Cross-account undo was blocked; check the account.')
                continue
            expected_accounts[e["id"]] = account
            back.setdefault((account, e["原文件夹"], e.get("原文件夹ID", "")), []).append(e["id"])
        for (account, folder, folder_id), ids in back.items():
            for i in range(0, len(ids), 50):
                batch = ids[i:i + 50]
                out, err = run_osascript(MOVE_SCRIPT, [folder, account, folder_id] + batch,
                                         timeout=300, verb="restore")
                if err:
                    return abort(translate(f'Undo interrupted: {err}; the full restore record was retained and actual locations remain unverified.'))
                try:
                    done, bad = parse_move_result(out, batch)
                    destinations.update(done)
                    unconfirmed.update(bad)
                except ValueError as exc:
                    reason = translate(f'The undo response could not be fully parsed: {exc}; success was not confirmed.')
                    warnings.append(translate(f'Folder “{folder}”: {reason}'))
                    unconfirmed.update({nid: reason for nid in batch})
        out, err = run_osascript(LIST_SCRIPT)
        if err:
            return abort(translate(f'Note locations could not be verified after undo: {err}; the full restore record was retained.'))
        try:
            current = {n["id"]: n for n in parse_notes(out)}
        except ValueError as exc:
            return abort(translate(f'The note location list could not be fully parsed after undo: {exc}; the full restore record was retained.'))
        succeeded, remaining, failures = [], [], []
        for e in entries:
            nid, position = e["id"], current.get(e["id"], {})
            actual = position.get("folder")
            reason = unconfirmed.get(nid)
            if (not reason and position.get("accountId") == expected_accounts.get(nid)
                    and destinations.get(nid) and position.get("folderId") == destinations[nid]):
                succeeded.append(e)
                continue
            if not reason:
                reason = (translate('The note was not found; it may have been deleted or become inaccessible.') if actual is None else
                          translate(f'The current account or folder does not match the original location (“{actual}”); success was not confirmed.'))
            remaining.append(e)
            failures.append(failure(e, reason, actual))
        # 防止工具以外的程序改动归位表时，被本次旧快照覆盖。
        with open(RESTORE_FILE, "rb") as f:
            if f.read() != original_bytes:
                return abort(translate('The restore map changed during undo. Existing records were preserved; check before retrying.'))
        if succeeded:
            if remaining:
                rec["笔记"] = remaining  # 保留所有既有字段，仅收窄这一条记录的笔记清单。
            else:
                book["记录"].pop()
            save_restore_book(book)
        result = {"restored": len(succeeded), "failed": len(failures),
                  "failures": failures, "时间": rec.get("时间", ""),
                  "recordRetained": bool(remaining), "log": log_result(succeeded, failures)}
        if warnings:
            result["warning"] = "；".join(warnings)
        return result, 200
    except Exception as exc:
        return abort(translate(f'Undo was not completed: {exc}; unconfirmed restore records were retained.'))

class RevisionConflict(Exception):
    def __init__(self, rev):
        super().__init__(translate('Another window changed the library. Refresh before continuing; unsubmitted changes remain in local recovery storage.'))
        self.rev = rev

def check_revision(expected):
    # 调用方必须持有 WRITE_LOCK，直到提交结束，禁止校验与写入之间插入另一笔写入。
    current = load_data()["rev"]
    if type(expected) is not int or expected < 0 or expected != current:
        raise RevisionConflict(current)
    return current

def atomic_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

def preserve_history(current_rev):
    """提交前版本：最近10次 + 最近12个有保存的小时首份，最多22份。
    只压缩一次并复用；旧JSON不迁移，新旧格式统一参与原有滚动清理。
    """
    if not os.path.exists(DATA_FILE):
        return
    os.makedirs(HISTORY_DIR, exist_ok=True)
    name = f"recent_{time.time_ns():020d}_{uuid.uuid4().hex}_rev{current_rev}.json.gz"
    hourly = os.path.join(HISTORY_DIR, f"hourly_{datetime.now():%Y%m%d-%H}.json")
    with open(DATA_FILE, "rb") as f:
        compressed = gzip.compress(f.read(), compresslevel=1, mtime=0)

    def write_compressed(path):
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(compressed)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    write_compressed(os.path.join(HISTORY_DIR, name))
    if not os.path.exists(hourly) and not os.path.exists(hourly + ".gz"):
        write_compressed(hourly + ".gz")
    for prefix, limit in (("recent_", HISTORY_RECENT_LIMIT), ("hourly_", HISTORY_HOURLY_LIMIT)):
        names = sorted(n for n in os.listdir(HISTORY_DIR)
                       if n.startswith(prefix) and n.endswith((".json", ".json.gz")))
        for old in names[:-limit]:
            os.remove(os.path.join(HISTORY_DIR, old))

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def handle_one_request(self):
        token = REQUEST_LANGUAGE.set("en")
        try:
            return super().handle_one_request()
        finally:
            REQUEST_LANGUAGE.reset(token)

    def parse_request(self):
        valid = super().parse_request()
        if valid:
            REQUEST_LANGUAGE.set(request_language(self.headers.get("X-UI-Language")))
        return valid

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, obj, status=200):
        if status >= 400 and isinstance(obj, dict) and isinstance(obj.get("error"), str):
            obj = {**obj, "error": translate(obj["error"])}
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def valid_host(self):
        hosts = self.headers.get_all("Host", [])
        return len(hosts) == 1 and hosts[0].lower() in (f"localhost:{PORT}", f"127.0.0.1:{PORT}")

    def allow_host(self):
        if self.valid_host():
            return True
        self.send_json({"error": "This request is not addressed to the local organizer."}, 403)
        return False

    def allow_write(self):
        if not self.allow_host():
            return False
        origins = self.headers.get_all("Origin", [])
        host = self.headers["Host"].lower()
        tokens = self.headers.get_all("X-Session-Token", [])
        if len(origins) != 1 or origins[0].lower() != f"http://{host}" or len(tokens) != 1 or not secrets.compare_digest(tokens[0], SESSION_TOKEN):
            self.send_json({"error": "This write request did not pass local session verification."}, 403)
            return False
        return True

    def serve_static(self, head_only=False):
        path = urlparse(self.path).path
        if path == "/":
            self.path = "/app.html"
        elif path not in STATIC_PATHS:
            self.send_error(404, "File not found")
            return
        if head_only:
            super().do_HEAD()
        else:
            super().do_GET()

    def do_HEAD(self):
        if self.allow_host():
            self.serve_static(head_only=True)

    def do_GET(self):
        if not self.allow_host():
            return
        if self.path == "/api/ping":
            touch_ping()
            self.send_json({"ok": True})
            return
        elif self.path == "/api/session/token":
            self.send_json({"token": SESSION_TOKEN})
        elif self.path == "/api/locale":
            self.send_json(system_locale())
        elif self.path.startswith("/api/diag?"):
            proj = (parse_qs(urlparse(self.path).query).get("project") or [""])[0]
            data = load_data()
            if proj not in data.get("projects", []):
                self.send_json({"error": f"No category named {proj!r} was found."}, 404)
                return
            folder = project_dir(proj)
            files = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
            self.send_json({
                "folder": folder,
                "exists": os.path.isdir(folder),
                "files": len(files),
                "active": sum(1 for f in files if ACTIVE_TAG in f),
            })
        elif self.path.startswith("/api/mirror?"):
            q = parse_qs(urlparse(self.path).query)
            pid = (q.get("id") or [""])[0]
            try:
                ver = int((q.get("v") or ["0"])[0])
            except ValueError:
                ver = 0
            rel = mirror_plan(load_data()).get((pid, ver))
            if not rel:
                self.send_json({"error": "No text mirror exists for this version. Save once to generate it."}, 404)
                return
            full = os.path.join(MIRROR_DIR, rel)
            self.send_json({"path": full, "exists": os.path.exists(full)})
        elif self.path in ("/api/notes/restore/info", "/api/notes/restore/history"):
            try:
                history = read_restore_history()
            except (OSError, ValueError, TypeError):
                self.send_json({"error": translate('Movement history could not be read because the restore map is inaccessible or damaged. The original file was not modified.')}, 500)
                return
            if self.path == "/api/notes/restore/history":
                self.send_json(history)
                return
            last = next((rec for rec in history["records"] if rec["index"] == history["nextIndex"]), None)
            self.send_json({"有记录": last is not None,
                            "时间": last["time"] if last else "",
                            "条数": last["count"] if last else 0,
                            "说明": last["description"] if last else "",
                            "同时间批数": last["sameTimeCount"] if last else 0,
                            "撤销后同时间剩余批数": last["sameTimeRemaining"] if last else 0,
                            "记录总数": history["total"], "索引": history["nextIndex"]})
        elif self.path == "/api/jev/config":
            conf = jev_classify.load_conf(LIB_DIR)
            # Report only whether a key is stored, never the key itself.
            self.send_json({"hasKey": bool(jev_classify.get_key(LIB_DIR)),
                            "descriptions": conf.get("descriptions", {}),
                            "skipDescription": conf.get("skipDescription", []),
                            "neverSend": conf.get("neverSend", []),
                            "sendSensitive": bool(conf.get("sendSensitive")),
                            "useBuiltinSecrets": bool(conf.get("useBuiltinSecrets", True)),
                            "customSecrets": conf.get("customSecrets", []),
                            "builtinLabels": [translate(label) for label, _ in jev_classify.BUILTIN_SECRETS],
                            "autoThreshold": conf["autoThreshold"],
                            "doubtThreshold": conf["doubtThreshold"],
                            "minGap": conf["minGap"]})
        elif self.path == "/api/data":
            self.send_json(load_data())
        elif self.path == "/api/session/report":
            report = session_report()
            self.send_json(report, 503 if "error" in report else 200)
        elif self.path == "/api/notes/list":
            notes, err = list_notes()
            if err:
                self.send_json({"error": err}, 500)
                return
            self.send_json({"notes": notes})
        elif self.path == "/api/notes/folder-details":
            folders, err = list_folder_details()
            if err:
                self.send_json({"error": err}, 500)
                return
            self.send_json({"folders": folders})
        elif self.path == "/api/notes/folders":
            out, err = run_osascript(FOLDERS_SCRIPT)
            if err:
                self.send_json({"error": err}, 500)
                return
            folder_text, _, default_text = (out or "").partition(RS)
            default_parts = default_text.split(US)
            default_folder = default_parts[0] if len(default_parts) == 2 else ""
            default_id = default_parts[1] if len(default_parts) == 2 else ""
            skip = {"Recently Deleted", "最近删除", "最近刪除"}
            folders = [f for f in folder_text.split(US) if f and f not in skip]
            seen, uniq = set(), []
            for f in folders:
                if f not in seen:
                    seen.add(f)
                    uniq.append(f)
            self.send_json({"folders": uniq, "defaultFolder": default_folder,
                            "defaultFolderId": default_id})
        else:
            self.serve_static()

    def do_POST(self):
        if not self.allow_write():
            return
        if self.path == "/api/save":
            data = self.read_body()
            try:
                result = save_data(data)
            except RevisionConflict as e:
                self.send_json({"error": str(e), "code": "REV_CONFLICT", "rev": e.rev}, 409)
            except Exception as e:
                self.send_json({"error": translate(f'Save failed: {e}')}, 500)
            else:
                self.send_json(result)
        elif self.path == "/api/notes/body":
            ids = self.read_body().get("ids", [])
            if not ids:
                self.send_json({"results": []})
                return
            out, err = run_osascript(BODY_SCRIPT, ids)
            if err:
                self.send_json({"error": err}, 500)
                return
            try:
                results = parse_body_results(out, ids)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 502)
                return
            self.send_json({"results": results})
        elif self.path == "/api/jev/config":
            b = self.read_body()
            key = (b.get("apiKey") or "").strip()
            if key and not key.isascii():
                self.send_json({"error": "An API key is ASCII only. Non-ASCII characters mean "
                                         "the copied value contains masking or nearby text."}, 400)
                return
            def clamp(name, fallback):
                try:
                    return min(1.0, max(0.0, float(b.get(name, fallback))))
                except (TypeError, ValueError):
                    return fallback
            # Partial settings updates must preserve omitted fields, including
            # keyword rules and folder exclusions configured elsewhere.
            incoming = {"apiKey": key}
            for name, fallback in (("autoThreshold", 0.75), ("doubtThreshold", 0.45), ("minGap", 0.15)):
                if name in b:
                    incoming[name] = clamp(name, fallback)
            for name in ("sendSensitive", "useBuiltinSecrets"):
                if name in b:
                    incoming[name] = bool(b[name])
            for optional in ("descriptions", "keywords", "neverSend", "skipDescription", "customSecrets"):
                if optional in b:
                    incoming[optional] = b[optional]
            with WRITE_LOCK:
                conf = jev_classify.save_conf(LIB_DIR, incoming)
            self.send_json({"ok": True, "hasKey": bool(jev_classify.get_key(LIB_DIR)),
                            "descriptions": conf.get("descriptions", {})})
        elif self.path == "/api/jev/classify":
            b = self.read_body()
            items = b.get("items", [])
            if not items:
                self.send_json({"rows": []})
                return
            descriptions = jev_classify.load_conf(LIB_DIR).get("descriptions", {})
            projects = [(name, descriptions.get(name, "")) for name in b.get("projects", [])]

            def fetch_bodies(ids):
                out, err = run_osascript(BODY_SCRIPT, ids)
                if err:
                    return None, err
                try:
                    results = parse_body_results(out, ids)
                except ValueError as exc:
                    return None, str(exc)
                failed = [row for row in results if not row["ok"]]
                if failed:
                    return None, "Could not read " + ", ".join(row["id"] for row in failed) + ". Please retry."
                return [row["body"] for row in results], None

            rows, err = jev_classify.classify(LIB_DIR, items, projects, fetch_bodies,
                                              REQUEST_LANGUAGE.get())
            if err:
                self.send_json({"error": err}, 400)
                return
            for row in rows:
                # Replace only the known authored prefix; preserve custom terms verbatim.
                reason = row.get("error")
                prefix = "Contains your own term "
                if isinstance(reason, str) and reason.startswith(prefix):
                    row["error"] = "Contains a custom term " + reason[len(prefix):]
            self.send_json({"rows": rows,
                            "missingDescriptions": [n for n, d in projects if not d]})
        elif self.path == "/api/notes/push":
            b = self.read_body()
            title = (b.get("title") or "Untitled").strip() or "Untitled"
            folder = (b.get("folder") or "Notes Organizer").strip() or "Notes Organizer"
            html = note_html(title, b.get("content") or "")
            args = [b.get("noteId") or "", folder, title, html]
            out, err = run_osascript(PUSH_SCRIPT, args, verb="write to")
            if err:
                self.send_json({"error": err}, 500)
                return
            nid = (out or "").strip()
            if not nid:
                self.send_json({"error": "Apple Notes did not return a note ID. The send may have failed; check Notes to confirm."}, 500)
                return
            self.send_json({"id": nid})
        elif self.path == "/api/mirror/reveal":
            b = self.read_body()
            rel = mirror_plan(load_data()).get((b.get("id"), b.get("v")))
            if not rel:
                self.send_json({"error": "No text mirror was found for this version."}, 404)
                return
            full = os.path.join(MIRROR_DIR, rel)
            if not os.path.exists(full):
                self.send_json({"error": "The text mirror has not been generated yet. Save once in the organizer first."}, 404)
                return
            subprocess.run(["open", "-R", full], capture_output=True)
            self.send_json({"ok": True, "path": full})
        elif self.path == "/api/notes/move":
            b = self.read_body()
            with WRITE_LOCK:
                r, err = move_notes(b.get("moves", []), b.get("label", ""), b.get("logKey"))
            if err:
                self.send_json({"error": err}, 500)
                return
            self.send_json(r)
        elif self.path == "/api/notes/restore":
            result, status = restore_notes()
            self.send_json(result, status)
        elif self.path == "/api/moves/reveal":
            if not os.path.isdir(MOVELOG_DIR):
                self.send_json({"error": "No movement logs exist yet. Organize notes once to create a log."}, 404)
                return
            subprocess.run(["open", MOVELOG_DIR], capture_output=True)
            self.send_json({"ok": True, "path": MOVELOG_DIR})
        elif self.path == "/api/session/reveal":
            path = SESSION_STATE["path"] if SESSION_STATE else None
            if not path or not os.path.isfile(path):
                self.send_json({"error": "No session log file is available."}, 404)
                return
            opened = subprocess.run(["open", "-R", path], capture_output=True)
            if opened.returncode:
                self.send_json({"error": "The session log could not be revealed in Finder."}, 500)
                return
            self.send_json({"ok": True, "path": path})
        elif self.path == "/api/diag/reveal":
            folder = project_dir(self.read_body().get("project", ""))
            if not os.path.isdir(folder):
                self.send_json({"error": "This category has no text mirror folder yet. Save once in the organizer first."}, 404)
                return
            subprocess.run(["open", folder], capture_output=True)
            self.send_json({"ok": True})
        elif self.path == "/api/similar/scan":
            b = self.read_body()
            conf = {k: b[k] for k in ("threshold", "maxRatio", "minLength") if k in b}
            result = similar_groups.scan(load_data(), conf)
            record = similar_groups.load_record(LIB_DIR)
            previous = (record or {}).get("groups", [])
            result["lastMerge"] = ({"groups": len(previous),
                                    "notes": sum(len(g["before"]) for g in previous)}
                                   if previous else None)
            self.send_json(result)
        elif self.path in ("/api/similar/merge", "/api/similar/undo"):
            body = self.read_body()
            try:
                with WRITE_LOCK:
                    check_revision(body.get("rev"))
                    operation = "merge" if self.path.endswith("/merge") else "undo"
                    if operation == "merge" and not body.get("groups"):
                        self.send_json({"error": "Nothing selected."}, 400)
                        return
                    stat = save_similar_change(operation, body.get("groups"))
                    if stat.get("error"):
                        self.send_json({"error": stat["error"]}, 400)
                        return
                    if not stat.get("groups"):
                        self.send_json({"error": "No group has two entries still in the library."}, 400)
                        return
            except RevisionConflict as exc:
                self.send_json({"error": str(exc), "code": "REV_CONFLICT", "rev": exc.rev}, 409)
            except Exception as exc:
                self.send_json({"error": f"Save failed: {exc}"}, 500)
            else:
                self.send_json({"ok": True, **stat})
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def migrate_legacy_names():
    """Give the library and its contents their English names.

    Renaming only. Nothing is copied, merged or rewritten, so a failure part way
    through leaves the remaining items under their old names and the next start
    finishes the job. When a current name already exists the legacy one is left
    alone and reported: guessing which of two libraries to keep is the user's
    decision, not ours."""
    for legacy in LEGACY_LIB_DIRS:
        if not os.path.isdir(legacy):
            continue
        if os.path.exists(LIB_DIR):
            print(f"Both {os.path.basename(legacy)} and {os.path.basename(LIB_DIR)} exist. "
                  f"The application reads only {os.path.basename(LIB_DIR)}. "
                  f"Inspect {os.path.basename(legacy)} before deciding whether to remove it manually.")
            continue
        try:
            os.rename(legacy, LIB_DIR)
            print(f"Renamed the data folder {os.path.basename(legacy)} to {os.path.basename(LIB_DIR)}.")
        except OSError as e:
            print(f"Could not rename {os.path.basename(legacy)} to {os.path.basename(LIB_DIR)}: {e}")
            return

    if not os.path.isdir(LIB_DIR):
        return

    for legacy_name, current_name in LEGACY_ENTRIES:
        legacy = os.path.join(LIB_DIR, legacy_name)
        current = os.path.join(LIB_DIR, current_name)
        if not os.path.exists(legacy) or os.path.exists(current):
            continue
        try:
            os.rename(legacy, current)
        except OSError as e:
            print(f"Could not rename {legacy_name} to {current_name}: {e}")

    # Archived snapshots and move logs keep their timestamps but take the new
    # naming, so a migrated library has no Chinese filenames left in Finder.
    # Snapshot pruning keeps the newest ten by sorted filename, which only
    # works while every name uses one scheme.
    _rename_archived(SNAP_DIR, re.compile(r"^库_(?P<stamp>[\d-]+)_(?P<count>\d+)条\.json$"),
                     "library_{stamp}_{count}-entries.json")
    _rename_archived(MOVELOG_DIR, re.compile(r"^移动记录_(?P<stamp>.+)\.md$"),
                     "move-log_{stamp}.md")


def _rename_archived(folder, pattern, template):
    if not os.path.isdir(folder):
        return
    for name in os.listdir(folder):
        match = pattern.match(name)
        if not match:
            continue
        target = os.path.join(folder, template.format(**match.groupdict()))
        if os.path.exists(target):
            continue
        try:
            os.rename(os.path.join(folder, name), target)
        except OSError:
            pass


def migrate_mirror_names(data):
    """Rewrite mirror filenames that still carry the old Chinese markers.

    rebuild_mirror() already converges the folder on the names it wants, but it
    only runs on save; without this an untouched library would keep showing
    _现役 and 未分类 in Finder until the user happened to edit something."""
    if not os.path.isdir(MIRROR_DIR):
        return
    stale = os.path.join(MIRROR_DIR, LEGACY_UNFILED_DIR)
    if os.path.isdir(stale) and not os.path.exists(os.path.join(MIRROR_DIR, UNFILED_DIR)):
        try:
            os.rename(stale, os.path.join(MIRROR_DIR, UNFILED_DIR))
        except OSError:
            pass
    for dirpath, _, filenames in os.walk(MIRROR_DIR):
        if any(LEGACY_ACTIVE_TAG in fn for fn in filenames):
            rebuild_mirror(data)
            return


def main():
    migrate_legacy_names()
    os.makedirs(LIB_DIR, exist_ok=True)
    migrate_mirror_names(load_data())
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # If the organizer is already running, open its existing page.
        import urllib.request
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/api/data", timeout=3)
            print("The organizer is already running. Opening its page; you may close this window.")
            webbrowser.open(URL)
            return
        except Exception:
            print(f"Port {PORT} is occupied by another application.")
            print(f"Run lsof -i :{PORT} in Terminal to identify it. Stop it before launching again, or choose another PORT.")
            input("Press Return to close this window…")
            sys.exit(1)
    try:
        start_session()
    except (OSError, ValueError) as exc:
        print(f"Could not create the session log: {exc}")
    threading.Thread(target=watchdog, daemon=True).start()
    print(f"Apple Notes Organizer is running at: {URL}")
    print("Close the browser window to stop the server. You may also close this Terminal window.")
    if "--open" in sys.argv:
        webbrowser.open(URL)
    def stop_on_terminal_close(_signal, _frame):
        raise KeyboardInterrupt
    for kind in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(kind, stop_on_terminal_close)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            write_session_log()
        except (OSError, ValueError) as exc:
            print(f"Could not finish the session log: {exc}")
        server.server_close()


if __name__ == "__main__":
    main()
