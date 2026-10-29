"""Chapter quiz adapter, scoped to the observed student work-module protocol.

Only server-supplied forms and actual option labels are serialized. Submission
claims survive process restarts so an unknown POST outcome is never retried.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import urllib.parse
from pathlib import Path

from playback_simulator.chaoxing_client import RemoteError, card_setting, validate_setting
from playback_simulator.simulator import atomic_json
from .platform import ChapterUnavailable, PlatformVerificationRequired, add_query, scoped_link
from .work_form import TaskForm


def entry_url(setting, study, attachment):
    try:
        validate_setting(setting)
    except RemoteError:
        raise ChapterUnavailable("章节测验所在卡片要求验证或特殊流程，请在原网页处理") from None
    defaults, prop = setting.get("defaults", {}), attachment.get("property", {})
    modules = [m for m in study.get("workModules", [])
               if str(m.get("mid", "")) == str(prop.get("mid", ""))
               and str(m.get("jobid", "")) == str(attachment.get("jobid", ""))]
    if len(modules) != 1 or not setting.get("control"):
        raise ChapterUnavailable("章节测验模块参数暂不支持，请在原网页处理")
    data = modules[0]
    if prop.get("workid") is not None and str(prop["workid"]) != str(data.get("workid", "")):
        raise ChapterUnavailable("章节测验模块与任务标识不一致")
    if (not all(str(defaults.get(k, "")) for k in ("courseid", "clazzId", "knowledgeid", "cpi"))
            or not data.get("workid") or not data.get("mid") or not data.get("_jobid")
            or not attachment.get("enc") or not study.get("utEnc")
            or str(data.get("courseid") or defaults["courseid"]) != str(defaults["courseid"])):
        raise ChapterUnavailable("章节测验缺少原页面凭证，请重新读取或在原网页处理")
    card = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(study["cards_url"]).query))
    if any(str(defaults[k]) != card.get(q) for k, q in
           (("courseid", "courseid"), ("clazzId", "clazzid"), ("knowledgeid", "knowledgeid"))):
        raise ChapterUnavailable("章节测验与当前卡片身份不一致")
    work_id = str(data["workid"])
    if "schoolid" in data:
        work_id = str(data["schoolid"]) + "-" + work_id
    values = {"api": "1", "workId": work_id, "jobid": data.get("jobid", ""),
              "originJobId": data["_jobid"], "needRedirect": "true", "skipHeader": "true",
              "knowledgeid": defaults["knowledgeid"], "ktoken": defaults.get("ktoken", ""),
              "cpi": defaults["cpi"], "ut": "s", "clazzId": defaults["clazzId"],
              "type": "b" if data.get("worktype") == "workB" else "",
              "enc": attachment["enc"], "utenc": study["utEnc"], "courseid": defaults["courseid"]}
    if attachment.get("workExtInfoEnc") and attachment.get("oriNodeId"):
        values.update(workExtInfoEnc=attachment["workExtInfoEnc"], oriNodeId=attachment["oriNodeId"])
    if str(setting.get("mooc2")) == "1":
        values["mooc2"] = "1"
    return add_query(scoped_link(study["cards_url"], "/mooc-ans/api/work"), values)


def checked_form(html, url, setting, study, attachment, *, allow_short_answer=False):
    try:
        form = TaskForm(html, url, allow_chapter_form=True, allow_short_answer=allow_short_answer)
    except (ValueError, TypeError):
        raise ChapterUnavailable("章节测验答题页不符合已支持的表单协议，请在原网页处理") from None
    fields, defaults = dict(form.fields), setting["defaults"]
    for name, key in (("courseId", "courseid"), ("classId", "clazzId"),
                      ("knowledgeid", "knowledgeid"), ("cpi", "cpi")):
        if fields.get(name) != str(defaults[key]) or sum(k == name for k, _ in form.fields) != 1:
            raise ChapterUnavailable("章节测验表单身份核对失败")
    jobs = {str(attachment.get("jobid", ""))}
    jobs.update(str(m.get("_jobid", "")) for m in study.get("workModules", [])
                if str(m.get("mid", "")) == str(attachment.get("property", {}).get("mid", "")))
    if not fields.get("jobid") or fields["jobid"] not in jobs:
        raise ChapterUnavailable("章节测验任务身份核对失败")
    if (not fields.get("workAnswerId", "").isdigit() or not fields.get("workRelationId", "").isdigit()
            or "exam" in urllib.parse.urlsplit(url).path.lower()
            or urllib.parse.urlsplit(form.action).hostname != urllib.parse.urlsplit(url).hostname):
        raise ChapterUnavailable("未识别为受支持的章节测验作答表单")
    inputs = {n.attrs.get("id"): n.attrs.get("value", "") for n in form.document.root.walk() if n.tag == "input"}
    try:
        if int(inputs.get("addTimes") or 0) > int(inputs.get("limitWorkSubmitTimes") or 100):
            raise ChapterUnavailable("章节测验需要提交验证码，请在原网页完成")
    except ValueError:
        raise ChapterUnavailable("无法确认章节测验提交验证状态") from None
    for qid in form.question_ids:
        if sum(k == "answer" + qid for k, _ in form.fields) != 1:
            raise ChapterUnavailable("章节测验答案控件暂不支持")
    return form


def choose_answers(form):
    fields, answers, randomized = dict(form.fields), {}, 0
    for qid in form.question_ids:
        existing = fields.get("answer" + qid, "").strip()
        if existing:
            if not form.valid_answer(qid, existing):
                raise ChapterUnavailable("已有作答或题型暂不支持，请在原网页确认")
            answers[qid] = existing
            continue
        options = form.options.get(qid, [])
        if not options or not all(form.valid_answer(qid, option) for option in options):
            raise ChapterUnavailable("包含未支持的题型或选项结构，请在原网页完成；未提交本测验")
        answers[qid] = secrets.choice(options)
        randomized += 1
    return answers, randomized


def confirmed(platform, study, attachment):
    html, _ = platform.request(study["cards_url"], study["url"])
    try:
        setting = card_setting(html)
    except Exception:
        return False
    matches = [a for a in setting.get("attachments", []) if a.get("type") in ("workid", "work")
               and str(a.get("jobid", "")) == str(attachment.get("jobid", ""))
               and str(a.get("property", {}).get("mid", "")) == str(attachment.get("property", {}).get("mid", ""))]
    return len(matches) == 1 and matches[0].get("isPassed") is True


def run_quiz(platform, setting, study, attachment, directory, *, cancel_event, answer_resolver=None):
    if cancel_event.is_set():
        return {"stopped": True}
    html, url = platform.request(entry_url(setting, study, attachment), study["cards_url"])
    if cancel_event.is_set():
        return {"stopped": True}
    form = checked_form(html, url, setting, study, attachment, allow_short_answer=answer_resolver is not None)
    fields = dict(form.fields)
    identity = [fields[k] for k in ("courseId", "classId", "cpi", "knowledgeid", "jobid", "workRelationId", "workAnswerId")]
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(json.dumps(identity).encode()).hexdigest() + ".json")
    # A prior claim is never a reason to POST again, even after server restart.
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ChapterUnavailable("测验提交记录无法核对，请在原网页检查，未重复提交") from None
        passed = confirmed(platform, study, attachment)
        if previous.get("status") == "accepted" or passed:
            return {"submitted": False, "alreadySubmitted": True, "isPassed": passed}
        raise ChapterUnavailable("此测验已有提交记录但结果未确认，请在原网页检查，未重复提交")
    if answer_resolver is None:
        answers, randomized = choose_answers(form)
    else:
        answers, randomized = answer_resolver(form), 0
    if cancel_event.is_set():
        return {"stopped": True}
    validation_url = add_query(scoped_link(url, "/mooc-ans/work/validate"),
                               {k: fields[k] for k in ("courseId", "classId", "cpi")})
    try:
        check = json.loads(platform.request(validation_url, url)[0])
    except (ValueError, TypeError):
        raise ChapterUnavailable("无法确认章节测验提交前校验状态，未提交") from None
    if not isinstance(check, dict) or check.get("status") != 3:
        raise ChapterUnavailable("章节测验要求验证或登录检查，请在原网页完成，未提交")
    try:
        request = form.prepare(answers)
    except (ValueError, TypeError):
        raise ChapterUnavailable("章节测验表单无法安全序列化，未提交") from None
    if cancel_event.is_set():
        return {"stopped": True}
    record = {"status": "pending", "createdAt": int(time.time()), "questionCount": len(answers), "randomizedCount": randomized}
    try:
        with path.open("x", encoding="utf-8") as claim:
            json.dump(record, claim)
    except FileExistsError:
        raise ChapterUnavailable("测验已有提交记录，未重复提交") from None
    try:
        # No retries and no redirect-following on this write.
        body, _ = platform.submit_form(request)
        result = json.loads(body)
        if not isinstance(result, dict) or result.get("status") is not True:
            record["status"] = "rejected"
            atomic_json(path, record)
            raise ChapterUnavailable("平台未接受测验提交，请在原网页查看原因，未自动重试")
        record["status"] = "accepted"
        record["acceptedAt"] = int(time.time())
        atomic_json(path, record)
    except PlatformVerificationRequired:
        raise
    except ChapterUnavailable:
        raise
    except Exception:
        raise ChapterUnavailable("测验提交结果不明，请在原网页检查；已保留提交记录，未自动重试") from None
    try:
        passed = confirmed(platform, study, attachment)
    except PlatformVerificationRequired:
        return {"submitted": True, "isPassed": False, "actionRequired": "platform_verification"}
    except RemoteError:
        # Acceptance is known even if the subsequent read is temporarily unavailable.
        passed = False
    return {"submitted": True, "isPassed": passed, "randomizedCount": randomized}
