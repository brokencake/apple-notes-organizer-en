"""UI language selection and translations of server-authored messages only."""
import os
import plistlib
import re
import subprocess
from contextvars import ContextVar
from functools import lru_cache

REQUEST_LANGUAGE = ContextVar("request_language", default="en")


def language_for(locale):
    prefix = str(locale or "").replace("_", "-").lower().split("-")[0]
    return prefix if prefix in {"en", "zh"} else None


@lru_cache(maxsize=1)
def system_locale():
    """Read preferred language tags, retaining no unrelated system preferences."""
    try:
        result = subprocess.run(["defaults", "export", "NSGlobalDomain", "-"],
                                capture_output=True, check=True, timeout=5)
        preferences = plistlib.loads(result.stdout)
        preferred = preferences.get("AppleLanguages", [])
        if isinstance(preferred, list):
            for locale in preferred:
                if isinstance(locale, str) and locale.strip():
                    return {"language": "zh" if language_for(locale) == "zh" else "en", "locale": locale,
                            "source": "system"}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        pass
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        locale = os.environ.get(name, "").split(".")[0].split("@")[0]
        language = language_for(locale)
        if language:
            return {"language": language, "locale": locale, "source": "environment"}
    return {"language": "en", "locale": "en", "source": "fallback"}


def request_language(header):
    return language_for(header) or system_locale()["language"]


# Format placeholders match rendered server messages. User content is never
# translated: callers use this only for known UI errors and report explanations.
MESSAGES = {
    "This request is not addressed to the local organizer.": "请求地址不是本机整理器。",
    "This write request did not pass local session verification.": "写入请求未通过本次使用的本机校验。",
    "Apple Notes returned an incomplete body response.": "备忘录正文读取结果不完整。",
    "Apple Notes returned the wrong number of note bodies.": "备忘录正文返回数量不一致。",
    "Apple Notes returned an invalid body status.": "备忘录正文读取状态无效。",
    "There is no merge to undo.": "没有可撤销的合并记录。",
    'API keys (sk-…)': 'API 密钥（sk-…）',
    'TypeSafe keys (apikey_…)': 'TypeSafe 密钥（apikey_…）',
    'Notion tokens': 'Notion 密钥',
    'GitHub tokens': 'GitHub 密钥',
    'AWS access key IDs': 'AWS 访问密钥 ID',
    'Any 32+ character token': '32 字符以上的连续凭据',
    'National ID numbers': '身份证号码',
    'US Social Security numbers': '美国社会安全号码',
    'Lines shaped like "password: …"': '形如“password: …”的行',
    'Account and password pairs (email---password)': '账号与密码组合（邮箱---密码）',
    'Folder "{folder}" is on the never-send list': '文件夹「{folder}」在不发送名单中',
    'Contains your own term "{word}"': '含有自定义词「{word}」',
    'Stopped on this Mac, not sent.': '已在本机拦截，没有发送。',
    'Nothing selected.': '尚未选择要合并的组。',
    'No group has two entries still in the library.': '没有任何组在库中仍保留至少两条笔记。',
    'Edit content in the organizer browser interface. Direct edits to these {ext} files are overwritten on the next save.': '请在整理器网页中编辑内容，直接修改这些 {ext} 文件会在下次保存时被覆盖。',

    "The request to {verb} Apple Notes timed out. Please try again.": "{verb}备忘录超时，请重试。",
    "Permission to {verb} Apple Notes was denied. Open System Settings → Privacy & Security → Automation, allow Terminal to control Notes, then try again.": "没有权限{verb}备忘录。请打开 系统设置 → 隐私与安全性 → 自动化，允许「终端」控制「备忘录」，然后重试。",
    "Could not {verb} Apple Notes: {error}": "备忘录{verb}失败：{error}",
    "Save failed: {error}": "保存失败：{error}",
    "The library was saved, but the session log could not be updated: {arg0}": "库已保存，但本次使用记录未能更新：{arg0}",
    "The selected destination folder changed or belongs to another account. No move was made.": "目标文件夹已变化或属于其他账户，未移动。",
    "The folder list is incomplete or contains duplicate folder IDs.": "文件夹清单不完整或含重复 ID。",
    "No project named {project} was found.": "找不到分类 {project}。",
    "No text mirror exists for this version. Save once to generate it.": "找不到这一版对应的文本镜像，保存一次即可生成。",
    "No text mirror was found for this version.": "找不到这一版对应的文本镜像。",
    "The text mirror has not been generated yet. Save once in the organizer first.": "文本镜像还没生成，请先在整理器中保存一次。",
    "Apple Notes did not return a note ID. The send may have failed; check Notes to confirm.": "备忘录没有返回笔记 ID，推送可能未成功，请到备忘录中确认。",
    "There is no recorded move to undo.": "没有可还原的移动记录。",
    "No movement logs exist yet. Organize notes once to create a log.": "尚无移动记录，整理过一次后才会生成。",
    "This project has no text mirror folder yet. Save once in the organizer first.": "此分类尚无 文本镜像文件夹，请先在整理器中保存一次。",
    "Original note locations recorded before moving, for undo": "移动备忘录前记录的原位置，用于还原",
    "# Notes movement log · {time}": "# 备忘录移动记录 · {time}",
    "> Only folders changed. Note content was not edited, and no notes were deleted.": "> 只移动文件夹，未修改正文，也未删除任何笔记。",
    "> To undo: Organizer → Organize Notes by category → Undo the last move.": "> 如需还原：整理器 → 按分类整理备忘录 → 还原上次。",
    "> To undo: Organizer → Organize Notes with Jev → Undo last batch (repeat for earlier batches).": "> 如需还原：整理器 → 用 Jev 整理备忘录 → 还原上批（可重复操作以还原更早的批次）。",
    "**{count}** notes in total.": "共 **{count}** 条。",
    "| Note | From | To |": "| 笔记 | 从 | 到 |",
    "Organize Notes by category": "按分类整理备忘录",
    "Undo: revert the {time} operation ({label})": "还原：撤销 {time} 的操作（{label}）",
    "This folder is generated by Apple Notes Organizer and synchronized on every save.": "此文件夹由备忘录整理器自动生成，每次保存时同步。",
    "Edit content in the organizer browser interface. Direct edits to these text files are overwritten on the next save.": "请在整理器网页中编辑内容，直接修改这些文本文件会在下次保存时被覆盖。",
}


# Account-safe moves, undo verification, and revision-protected saves.
MESSAGES.update({'The folder account could not be verified. No move was made.': '无法确认文件夹所属账户，未移动',
 'The folder hierarchy could not be resolved. No move was made.': '文件夹层级无法解析，未移动',
 'The folder account could not be verified.': '无法确认文件夹所属账户',
 'The folder hierarchy could not be resolved.': '文件夹层级无法解析',
 'Move parameters are missing an account boundary.': '移动参数缺少账户边界',
 'The source account could not be verified. No move was made.': '无法确认来源账户，未移动',
 'The note account changed. A cross-account move was blocked; reopen the preview.': '笔记账户已变化，未跨账户移动；请重新预览核对',
 'The original folder belongs to another account. Cross-account undo was blocked.': '原文件夹属于另一账户，未跨账户还原',
 'This account has multiple folders with the same name. Rename them before retrying; no move was made.': '同一账户有多个同名文件夹；请改名区分后重试，本次未移动',
 'The source account changed during execution. No move was made.': '来源账户在执行时变化，未移动',
 'The destination account could not be verified. No move was made.': '目标账户无法确认，未移动',
 ' Confirmed restored: **{arg0}**; unconfirmed: **{arg1}**.': ' 已逐条确认还原 **{arg0}** 条；未确认还原 **{arg1}** '
                                                              '条。',
 '\n\nThe table below contains only individually confirmed successes.': '\n\n下表仅列已逐条确认成功的笔记。',
 'The note account could not be verified. No move was made; reopen the preview.': '无法确认笔记所属账户，未移动；请重新预览核对',
 'Sync back to phone': '反向同步手机备忘录',
 'The library revision is invalid. Check the library file before saving.': '库版本号无效，请先检查库文件，不能覆盖保存',
 'Move counts could not be parsed or do not match the batch size.': '移动返回计数无法解析或与批次数量不符',
 'Individual move receipts do not match the counts.': '移动逐条回执与计数不符',
 'The restore record format is invalid.': '归位表的记录格式无效',
 ' Failed or unconfirmed restore records were retained for retry.\n\n': ' 失败／未确认的归位记录已保留，可重试。\n\n',
 '| Unconfirmed note | id | Original folder | Reason |\n| --- | --- | --- | --- |\n': '| 未确认的笔记 | id | '
                                                                                      '原文件夹 | 原因 |\n'
                                                                                      '| --- | --- | '
                                                                                      '--- | --- |\n',
 'Undo: revert {arg0} ({arg1})': '还原：撤销 {arg0} 那次「{arg1}」',
 'Another window changed the library. Refresh before continuing; unsubmitted changes remain in local recovery storage.': '有别的窗口改过库了，这个窗口的版本已过期，请刷新后再操作；未提交改动仍保留在本机暂存。',
 'Original note locations recorded before moving, for undo': '移动备忘录前记下的原位置，用于一键还原',
 'The account differs from the preview. No move was made; reopen the preview.': '预览与当前账户不一致，未移动；请重新打开预览',
 'The library was saved, but the text mirror could not be updated: {arg0}': '库已保存，txt镜像暂未更新：{arg0}',
 'The note location list is malformed or contains duplicate IDs.': '笔记位置清单无法完整解析或含重复ID',
 'Restore batch {arg0} has an invalid timestamp or note list.': '归位表第 {arg0} 批的时间或笔记清单无效',
 'An undo is in progress. Wait for it to finish before retrying.': '正在还原，请等本次完成后再重试',
 'Untitled': '未命名',
 'The latest restore record has an invalid note list.': '最近一次归位记录的笔记清单无效',
 'Accounts could not be verified before undo: {arg0}; the full restore record was retained.': '还原前无法确认账户：{arg0}；整条归位记录已保留',
 'The current account could not be verified. No undo was made.': '无法确认当前账户，未还原',
 'The note is in another account. Cross-account undo was blocked; check the account.': '笔记已在另一账户，未跨账户还原；请核对账户',
 'Note locations could not be verified after undo: {arg0}; the full restore record was retained.': '还原后无法核实笔记位置：{arg0}；整条归位记录已保留',
 'Undo was not completed: {arg0}; unconfirmed restore records were retained.': '还原未完成：{arg0}；未确认成功的归位记录已保留',
 'Note account or folder metadata is incomplete.': '笔记账户或文件夹信息不完整',
 'Restore batch {arg0} has an invalid description or destination folder.': '归位表第 {arg0} 批的说明或目标文件夹无效',
 'Restore batch {arg0} contains an invalid note detail.': '归位表第 {arg0} 批存在无效的笔记明细',
 'Move to: ': '移动到：',
 'There is no recorded move to undo.': '没有可还原的记录',
 'The latest restore record contains invalid or duplicate note IDs or original folders.': '最近一次归位记录存在无效或重复的笔记 '
                                                                                          'id／原文件夹',
 'The note location list could not be fully parsed after undo: {arg0}; the full restore record was retained.': '还原后笔记位置清单无法完整解析：{arg0}；整条归位记录已保留',
 'The note was not found; it may have been deleted or become inaccessible.': '未找到这条笔记（可能已删除或无法访问）',
 'The current account or folder does not match the original location (“{arg0}”); success was not confirmed.': '当前账户／文件夹位置未匹配原位（「{arg0}」），未确认成功',
 'The restore map changed during undo. Existing records were preserved; check before retrying.': '还原期间归位表发生变化，未覆盖现有记录；请核对后重试',
 '{arg0}; the restore record was retained. Check the current location.': '{arg0}；归位记录已保留，请核对实际位置',
 'Restore batch {arg0} contains an invalid note-detail field.': '归位表第 {arg0} 批存在无效的笔记明细字段',
 'The latest restore record has invalid account or folder IDs.': '最近一次归位记录的账户／文件夹ID无效',
 'Undo interrupted: {arg0}; the full restore record was retained and actual locations remain unverified.': '还原中断：{arg0}；整条归位记录已保留，实际位置尚未确认',
 'The undo response could not be fully parsed: {arg0}; success was not confirmed.': '还原返回无法完整解析：{arg0}，未确认成功',
 'Folder “{arg0}”: {arg1}': '文件夹「{arg0}」：{arg1}',
 'Save failed: {arg0}': '保存失败：{arg0}',
 'Movement history could not be read because the restore map is inaccessible or damaged. The original file was not modified.': '无法读取移动记录：归位表无法读取或格式损坏；请检查归位表，原文件未修改。',
 'The note location response has an invalid format.': '笔记位置返回格式无效',
 'The move response has an invalid format.': '移动返回格式无效',
 'An individual move receipt is invalid.': '移动逐条回执无效',
 'Move': '移动',
 '> To undo: Organizer → Sync back to phone → Undo (repeat for earlier batches).': '> 如需还原：整理器 → '
                                                                                   '反向同步手机备忘录 → '
                                                                                   '还原（可重复还原更早批次）。',
 'No category named {project} was found.': '找不到分类 {project}。',
 'This category has no text mirror folder yet. Save once in the organizer first.': '此分类尚无文本镜像文件夹，请先在整理器中保存一次。',
 'An API key is ASCII only. Non-ASCII characters mean the copied value contains masking or nearby text.': 'API '
                                                                                                          '密钥仅支持 '
                                                                                                          'ASCII '
                                                                                                          '字符；非 '
                                                                                                          'ASCII '
                                                                                                          '字符表示复制内容包含掩码或旁边的文字。',
 'Contains a custom term "{word}"': '含有自定义词「{word}」'})


def _pattern(template):
    names = re.findall(r"\{(\w+)\}", template)
    pattern = re.escape(template)
    for name in names:
        pattern = pattern.replace(re.escape("{" + name + "}"), "(?P<" + name + ">.*?)", 1)
    return re.compile("^" + pattern + "$", re.DOTALL)


PATTERNS = [(_pattern(source), target) for source, target in MESSAGES.items()]


def translate(message, language=None):
    if (language or REQUEST_LANGUAGE.get()) != "zh":
        return message
    for pattern, target in PATTERNS:
        match = pattern.fullmatch(message)
        if match:
            values = match.groupdict()
            if "verb" in values:
                values["verb"] = {"read": "读取", "write to": "写入",
                                  "organize": "整理", "restore": "还原"}.get(values["verb"], values["verb"])
            return target.format(**values)
    return message
