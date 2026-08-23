#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude-hook-lark 单元测试。只用标准库，直接 python3 -m unittest 即可跑。"""

import contextlib
import io
import json
import os
import sys
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import lark_notify as ln  # noqa: E402


class TestSign(unittest.TestCase):
    def test_sign_is_deterministic_base64(self):
        s1 = ln.sign("mysecret", "1700000000")
        s2 = ln.sign("mysecret", "1700000000")
        self.assertEqual(s1, s2)
        self.assertNotEqual(s1, ln.sign("mysecret", "1700000001"))
        # base64 of a sha256 digest is always 44 chars with padding
        self.assertEqual(len(s1), 44)


class TestQuietHours(unittest.TestCase):
    def test_same_day_window(self):
        self.assertTrue(ln.in_quiet_hours([9, 18], now_hour=12))
        self.assertFalse(ln.in_quiet_hours([9, 18], now_hour=8))
        self.assertFalse(ln.in_quiet_hours([9, 18], now_hour=18))

    def test_wrapping_window(self):
        self.assertTrue(ln.in_quiet_hours([22, 8], now_hour=23))
        self.assertTrue(ln.in_quiet_hours([22, 8], now_hour=3))
        self.assertFalse(ln.in_quiet_hours([22, 8], now_hour=12))

    def test_invalid_input_is_not_quiet(self):
        for bad in (None, [], [1], "22-8", [5, 5], ["a", "b"]):
            self.assertFalse(ln.in_quiet_hours(bad, now_hour=3), bad)


class TestExtractText(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(ln.extract_text("hello"), "hello")

    def test_block_list(self):
        blocks = [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "第一段"},
            {"type": "tool_use", "name": "Bash"},
            {"type": "text", "text": "第二段"},
        ]
        self.assertEqual(ln.extract_text(blocks), "第一段\n第二段")

    def test_junk(self):
        self.assertEqual(ln.extract_text(None), "")
        self.assertEqual(ln.extract_text(42), "")


class TestTruncate(unittest.TestCase):
    def test_short_untouched(self):
        self.assertEqual(ln.truncate("abc", 10), "abc")

    def test_long_gets_ellipsis(self):
        out = ln.truncate("a" * 50, 10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))


class TestParseTranscript(unittest.TestCase):
    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_missing_file_is_empty_not_raising(self):
        stats = ln.parse_transcript("/definitely/not/here.jsonl")
        self.assertEqual(stats["assistant_text"], "")
        self.assertEqual(ln.parse_transcript("")["tool_calls"], 0)

    def test_last_turn_only(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
             "message": {"content": "第一个任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:05.000Z",
             "message": {"model": "claude-opus-5", "content": [
                 {"type": "tool_use", "name": "Bash"},
             ]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:06.000Z",
             "message": {"content": [{"type": "text", "text": "第一轮结果"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:01:00.000Z",
             "gitBranch": "feat/x", "message": {"content": "第二个任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:01:10.000Z",
             "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:01:11.000Z",
             "toolUseResult": {"ok": True}, "message": {"content": "工具结果不算新一轮"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:01:30.000Z",
             "message": {"content": [{"type": "text", "text": "第二轮结果"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "第二个任务")
        self.assertEqual(stats["assistant_text"], "第二轮结果")
        self.assertEqual(stats["tool_calls"], 1, "工具计数应在新一轮重置")
        self.assertEqual(stats["git_branch"], "feat/x")
        self.assertEqual(stats["model"], "claude-opus-5")
        self.assertAlmostEqual(ln.turn_duration(stats), 30.0, places=1)

    def test_meta_and_sidechain_users_do_not_start_a_turn(self):
        """skill 展开文本 / subagent 对话都是 type=user，不能被当成真人输入。"""
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
             "userType": "external", "message": {"content": "真人提的问题"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:02.000Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:00:03.000Z",
             "userType": "external", "isMeta": True,
             "message": {"content": [{"type": "text", "text": "Run an adversarial review..."}]}},
            {"type": "user", "timestamp": "2026-01-01T00:00:04.000Z",
             "userType": "external", "isSidechain": True,
             "message": {"content": "subagent 的输入"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:20.000Z",
             "message": {"content": [{"type": "text", "text": "干完了"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "真人提的问题")
        self.assertEqual(stats["tool_calls"], 1, "meta 记录不应重置工具计数")
        self.assertAlmostEqual(ln.turn_duration(stats), 20.0, places=1)

    def test_command_output_never_becomes_the_task_line(self):
        """<local-command-stdout> 是命令输出，既不是任务文本，也不构成轮次边界。"""
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
             "userType": "external", "message": {"content": "真正的任务描述"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:02.000Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:00:06.000Z", "userType": "external",
             "message": {"content": "<local-command-stdout>Set model to Opus</local-command-stdout>"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:30.000Z",
             "message": {"content": [{"type": "text", "text": "搞定"}]}},
        ])
        stats = ln.parse_transcript(path)
        # 这些标签没有可信字段能证明来自系统 —— 用户可以自己粘一段同样开头的文本。
        # 所以它构成完整边界：任务、回答、工具计数、起始时间一起断开。
        # 只清任务是不够的：上一轮回答里往往复述了任务内容。
        self.assertEqual(stats["user_prompt"], "")
        self.assertNotIn("Set model", stats["user_prompt"])
        self.assertEqual(stats["tool_calls"], 0, "边界后工具计数从头算")
        self.assertAlmostEqual(ln.turn_duration(stats), 24.0, places=1)

    def test_corrupt_lines_do_not_crash_and_recover(self):
        """开头的坏行不影响后续解析；结尾已落盘的坏行则 fail closed。"""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("not json at all\n")
            f.write("\n")
            f.write(json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "还是读到了"}]}}) + "\n")
        self.addCleanup(os.unlink, path)
        self.assertEqual(ln.parse_transcript(path)["assistant_text"], "还是读到了")

        fd2, path2 = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd2, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "会被清掉"}]}}) + "\n")
            f.write("[1,2,3]\n")      # 合法 JSON 但不是对象，且已落盘
        self.addCleanup(os.unlink, path2)
        self.assertEqual(ln.parse_transcript(path2)["assistant_text"], "")


class TestCleanUserText(unittest.TestCase):
    def test_system_reminder_is_stripped(self):
        raw = "帮我修一下登录\n<system-reminder>\nThe user named this session X\n</system-reminder>"
        self.assertEqual(ln.clean_user_text(raw), "帮我修一下登录")

    def test_multiple_reminders_stripped(self):
        raw = "<system-reminder>a</system-reminder>真正的任务<system-reminder>b</system-reminder>"
        self.assertEqual(ln.clean_user_text(raw), "真正的任务")

    def test_slash_command_records_are_dropped(self):
        for noise in (
            "<command-name>/clear</command-name>\n<command-message>clear</command-message>",
            "<local-command-stdout>Set model to Opus</local-command-stdout>",
            "<bash-input>ls -la</bash-input>",
            "<user-memory-input>记住这个</user-memory-input>",
        ):
            self.assertEqual(ln.clean_user_text(noise), "", noise[:30])

    def test_reminder_only_message_is_dropped(self):
        self.assertEqual(ln.clean_user_text("<system-reminder>only</system-reminder>"), "")

    def test_normal_text_untouched(self):
        self.assertEqual(ln.clean_user_text("  正常任务  "), "正常任务")
        self.assertEqual(ln.clean_user_text(""), "")


class TestShouldNotify(unittest.TestCase):
    def test_event_filter(self):
        ok, _ = ln.should_notify({"hook_event_name": "Stop"}, {"events": ["Stop"]})
        self.assertTrue(ok)
        ok, reason = ln.should_notify({"hook_event_name": "SessionEnd"}, {"events": ["Stop"]})
        self.assertFalse(ok)
        self.assertIn("SessionEnd", reason)

    def test_default_events(self):
        self.assertTrue(ln.should_notify({"hook_event_name": "Stop"}, {})[0])
        self.assertTrue(ln.should_notify({"hook_event_name": "Notification"}, {})[0])
        self.assertFalse(ln.should_notify({"hook_event_name": "SessionEnd"}, {})[0])

    def test_missing_event_name_defaults_to_stop(self):
        self.assertTrue(ln.should_notify({}, {})[0])

    def test_quiet_hours_blocks(self):
        cfg = {"events": ["Stop"], "quiet_hours": [0, 24]}
        # [0,24] 归一化后 start<end 覆盖全天
        ok, reason = ln.should_notify({"hook_event_name": "Stop"}, cfg)
        self.assertFalse(ok)
        self.assertIn("静默", reason)


class TestBuildCard(unittest.TestCase):
    def test_shape_and_color_fallback(self):
        card = ln.build_card("标题", "not-a-color", "正文", [ln.field("项目", "demo")], "footer")
        self.assertEqual(card["msg_type"], "interactive")
        self.assertEqual(card["card"]["header"]["template"], "blue")
        self.assertEqual(card["card"]["header"]["title"]["content"], "标题")
        tags = [e["tag"] for e in card["card"]["body"]["elements"]]
        self.assertEqual(tags, ["markdown", "hr", "div", "markdown"])

    def test_uses_card_schema_2_0(self):
        """v1 的 div+lark_md 渲染不了标题/列表/代码块，必须用 2.0 的 markdown 元素。"""
        card = ln.build_card("t", "green", "## 标题\n- 列表", [ln.field("k", "v")], "f")["card"]
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("body", card, "2.0 的元素挂在 body.elements 下")
        self.assertNotIn("elements", card, "顶层不该再有 elements")
        self.assertEqual(card["body"]["elements"][0]["tag"], "markdown")

    def test_never_emits_note_element(self):
        """note 在 card 2.0 里会被飞书拒绝（code 11246），实测过所有写法。"""
        card = ln.build_card("t", "green", "b", [ln.field("k", "v")], "footer")["card"]
        tags = [e["tag"] for e in card["body"]["elements"]]
        self.assertNotIn("note", tags)
        self.assertIn("footer", json.dumps(card, ensure_ascii=False), "脚注内容仍要在")

    def test_valid_color_kept(self):
        card = ln.build_card("t", "green", "b", [], "f")
        self.assertEqual(card["card"]["header"]["template"], "green")

    def test_payload_is_json_serializable_with_quotes_and_newlines(self):
        nasty = 'he said "hi"\n\\ backslash\ttab 中文'
        card = ln.build_card(nasty, "green", nasty, [ln.field("k", nasty)], nasty)
        roundtrip = json.loads(json.dumps(card, ensure_ascii=False))
        self.assertEqual(roundtrip["card"]["header"]["title"]["content"], nasty)


class TestBuildHookPayload(unittest.TestCase):
    def test_stop_event(self):
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(),
             "session_id": "abcdef123456", "transcript_path": "/nope"},
            {},
        )
        self.assertEqual(card["card"]["header"]["template"], "green")
        self.assertIn("任务完成", card["card"]["header"]["title"]["content"])
        json.dumps(card)  # 必须可序列化

    def test_notification_permission_is_orange(self):
        card = ln.build_hook_payload(
            {"hook_event_name": "Notification", "cwd": os.getcwd(),
             "message": "Claude needs your permission to use Bash"},
            {},
        )
        self.assertEqual(card["card"]["header"]["template"], "orange")

    def test_notification_idle_is_yellow(self):
        card = ln.build_hook_payload(
            {"hook_event_name": "Notification", "cwd": os.getcwd(),
             "message": "Claude is waiting for your input"},
            {},
        )
        self.assertEqual(card["card"]["header"]["template"], "yellow")

    def test_unknown_event_does_not_crash(self):
        card = ln.build_hook_payload({"hook_event_name": "WhoKnows", "cwd": os.getcwd()}, {})
        self.assertIn("Claude Code", card["card"]["header"]["title"]["content"])


class TestHumanDuration(unittest.TestCase):
    def test_units(self):
        self.assertEqual(ln.human_duration(5), "5 秒")
        self.assertEqual(ln.human_duration(65), "1 分 5 秒")
        self.assertEqual(ln.human_duration(3725), "1 小时 2 分")
        self.assertEqual(ln.human_duration(-3), "0 秒")


class TestSessionName(unittest.TestCase):
    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_ai_title_and_session_id_are_picked_up(self):
        path = self._write([
            {"type": "ai-title", "aiTitle": "飞书插件", "sessionId": "sess-1111"},
            {"type": "agent-name", "agentName": "飞书插件", "sessionId": "sess-1111"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["session_name"], "飞书插件")
        self.assertEqual(stats["session_id"], "sess-1111")

    def test_rename_keeps_the_last_title(self):
        path = self._write([
            {"type": "ai-title", "aiTitle": "旧名字", "sessionId": "s"},
            {"type": "ai-title", "aiTitle": "新名字", "sessionId": "s"},
        ])
        self.assertEqual(ln.parse_transcript(path)["session_name"], "新名字")

    def test_absent_title_is_empty_not_none(self):
        path = self._write([{"type": "assistant", "message": {"content": "hi"}}])
        self.assertEqual(ln.parse_transcript(path)["session_name"], "")

    def test_session_fields_shape(self):
        fields = ln.session_fields("3f2a9c10-1111-2222-3333-444455556666")
        self.assertEqual(len(fields), 1, "只保留会话 ID —— 会话名已经在标题里")
        self.assertTrue(fields[0]["is_short"], "与项目/分支/主机同排，不单独占一行")
        self.assertIn("3f2a9c10-1111-2222-3333-444455556666", fields[0]["text"]["content"])

    def test_all_meta_fields_are_short(self):
        """底部元信息排成两列，不让任何一项独占整行。"""
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(),
             "session_id": "abc-123", "transcript_path": "/nope"}, {})
        fields = card["card"]["body"]["elements"][2]["fields"]
        self.assertTrue(all(f["is_short"] for f in fields),
                        [f["text"]["content"] for f in fields])

    def test_session_fields_omit_missing_id(self):
        self.assertEqual(ln.session_fields(""), [])

    def test_session_id_falls_back_to_env(self):
        old = os.environ.get("CLAUDE_CODE_SESSION_ID")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "env-session-does-not-exist"
        try:
            sid, name = ln.session_context({})
            self.assertEqual(sid, "env-session-does-not-exist")
            self.assertEqual(name, "", "找不到 transcript 时会话名降级为空，不应抛异常")
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = old

    def test_payload_session_id_wins_over_env(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "from-env"
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        sid, _ = ln.session_context({"session_id": "from-payload"}, {"session_name": "n"})
        self.assertEqual(sid, "from-payload")

    def test_find_transcript_missing_returns_empty(self):
        self.assertEqual(ln.find_transcript(""), "")
        self.assertEqual(ln.find_transcript("no-such-session-id-xyz"), "")

    def test_hook_card_contains_session_id_and_name(self):
        path = self._write([
            {"type": "ai-title", "aiTitle": "我的会话", "sessionId": "abc-123"},
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
             "message": {"content": "任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:10.000Z",
             "message": {"content": [{"type": "text", "text": "完成"}]}},
        ])
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(),
             "session_id": "abc-123", "transcript_path": path},
            {},
        )
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("我的会话", blob)
        self.assertIn("abc-123", blob)

    def test_hook_card_keeps_session_even_when_summary_disabled(self):
        path = self._write([
            {"type": "ai-title", "aiTitle": "静默会话", "sessionId": "zzz"},
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
             "message": {"content": "机密任务描述"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:10.000Z",
             "message": {"content": [{"type": "text", "text": "机密结果"}]}},
        ])
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(),
             "session_id": "zzz", "transcript_path": path},
            {"include_summary": False},
        )
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("静默会话", blob)
        self.assertNotIn("机密任务描述", blob)
        self.assertNotIn("机密结果", blob)


class TestMask(unittest.TestCase):
    def test_secret_not_fully_revealed(self):
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/11112222-3333-4444-5555-666677778888"
        masked = ln.mask(url)
        self.assertNotIn("11112222-3333", masked)
        self.assertIn("***", masked)
        self.assertEqual(ln.mask(""), "(未配置)")


# ── codex 对抗审查回归用例 ──────────────────────────────────────────

SAFE_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/1111-2222-3333-SECRETTOKEN"
EVIL_URL = "https://attacker.example.com/collect"


class ConfigSandbox(unittest.TestCase):
    """把用户配置、项目配置和相关环境变量全部隔离到临时目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.user_cfg = Path(self.tmp) / "user.json"
        self.project = Path(self.tmp) / "proj"
        (self.project / ".claude").mkdir(parents=True)

        self._orig_cfg_path = ln.CONFIG_PATH
        ln.CONFIG_PATH = self.user_cfg
        self.addCleanup(lambda: setattr(ln, "CONFIG_PATH", self._orig_cfg_path))

        for var in ("LARK_WEBHOOK_URL", "FEISHU_WEBHOOK_URL", "LARK_WEBHOOK_SECRET",
                    "FEISHU_WEBHOOK_SECRET", "CLAUDE_HOOK_LARK_CONFIG"):
            if var in os.environ:
                old = os.environ.pop(var)
                self.addCleanup(os.environ.__setitem__, var, old)

    def write_user(self, data):
        self.user_cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_project(self, data):
        path = self.project / ".claude" / "lark.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                        encoding="utf-8")


class TestProjectConfigCannotHijackDestination(ConfigSandbox):
    """[high] 仓库里的 .claude/lark.json 不得指定通知目的地。"""

    def test_project_webhook_url_is_ignored(self):
        self.write_user({"webhook_url": SAFE_URL})
        self.write_project({"webhook_url": EVIL_URL, "secret": "evil"})
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["webhook_url"], SAFE_URL, "项目配置绝不能改写目的地")
        self.assertNotEqual(cfg.get("secret"), "evil")
        self.assertTrue(any("webhook_url" in w for w in cfg["_warnings"]),
                        "被忽略的目的地字段必须给出告警")

    def test_project_cannot_widen_allowed_hosts(self):
        self.write_user({"webhook_url": SAFE_URL})
        self.write_project({"allowed_hosts": ["attacker.example.com"]})
        cfg = ln.load_config(str(self.project))
        self.assertEqual(ln.allowed_hosts(cfg), ln.DEFAULT_ALLOWED_HOSTS)

    def test_project_cannot_inject_webhook_registry(self):
        self.write_user({"webhook_url": SAFE_URL})
        self.write_project({"webhooks": {"x": EVIL_URL}, "webhook": "x"})
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg.get("_fatal", "") != "", True, "引用未登记名称应停发")
        self.assertNotEqual(cfg.get("webhook_url"), EVIL_URL)

    def test_project_may_still_narrow_behaviour(self):
        self.write_user({"webhook_url": SAFE_URL, "events": ["Stop", "Notification"]})
        self.write_project({"events": ["Stop"], "quiet_hours": [22, 8], "include_summary": False})
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["events"], ["Stop"])
        self.assertEqual(cfg["quiet_hours"], [22, 8])
        self.assertIs(cfg["include_summary"], False)
        self.assertEqual(cfg["webhook_url"], SAFE_URL)

    def test_named_webhook_resolves_url_and_secret_together(self):
        self.write_user({
            "webhook_url": SAFE_URL,
            "secret": "default-secret",
            "webhooks": {"团队群": {"webhook_url": SAFE_URL + "-team", "secret": "team-secret"}},
        })
        self.write_project({"webhook": "团队群"})
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["webhook_url"], SAFE_URL + "-team")
        self.assertEqual(cfg["secret"], "team-secret", "URL 与密钥必须成对，不能错配")

    def test_unknown_named_webhook_stops_sending(self):
        self.write_user({"webhook_url": SAFE_URL})
        self.write_project({"webhook": "不存在的群"})
        cfg = ln.load_config(str(self.project))
        self.assertIn("_fatal", cfg)

    def test_broken_project_json_stops_instead_of_falling_back(self):
        self.write_user({"webhook_url": SAFE_URL})
        self.write_project("{ this is not json")
        cfg = ln.load_config(str(self.project))
        self.assertIn("_fatal", cfg, "项目配置损坏时必须停发，而不是悄悄发到全局群")

    def test_env_still_wins(self):
        self.write_user({"webhook_url": SAFE_URL})
        os.environ["LARK_WEBHOOK_URL"] = SAFE_URL + "-env"
        self.addCleanup(os.environ.pop, "LARK_WEBHOOK_URL", None)
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["webhook_url"], SAFE_URL + "-env")


class TestDestinationValidation(unittest.TestCase):
    def test_rejects_non_https(self):
        ok, why = ln.validate_webhook("http://open.feishu.cn/x")
        self.assertFalse(ok)
        self.assertIn("https", why)

    def test_rejects_unknown_host(self):
        ok, why = ln.validate_webhook(EVIL_URL)
        self.assertFalse(ok)
        self.assertIn("attacker.example.com", why)

    def test_accepts_feishu_and_larksuite(self):
        self.assertTrue(ln.validate_webhook(SAFE_URL)[0])
        self.assertTrue(ln.validate_webhook("https://open.larksuite.com/open-apis/bot/v2/hook/x")[0])

    def test_empty_and_garbage(self):
        self.assertFalse(ln.validate_webhook("")[0])
        self.assertFalse(ln.validate_webhook("not-a-url/SECRET")[0])

    def test_custom_allowlist_can_be_widened_by_user_config(self):
        hosts = ln.allowed_hosts({"allowed_hosts": ["internal.corp.example"]})
        self.assertTrue(ln.validate_webhook("https://internal.corp.example/hook", hosts)[0])


class TestNoWebhookLeakage(unittest.TestCase):
    """[medium] 任何错误路径都不得回显完整 webhook URL。"""

    TOKEN = "SECRETTOKEN"

    def test_malformed_url_error_hides_url(self):
        ok, why = ln.post("not-a-url/" + self.TOKEN, {"a": 1})
        self.assertFalse(ok)
        self.assertNotIn(self.TOKEN, why)

    def test_disallowed_host_error_hides_token(self):
        ok, why = ln.post("https://evil.example.com/hook/" + self.TOKEN, {"a": 1})
        self.assertFalse(ok)
        self.assertNotIn(self.TOKEN, why)

    def test_unreachable_host_error_hides_token(self):
        # 192.0.2.0/24 是 RFC 5737 保留的 TEST-NET-1，保证不可路由，
        # 测试不会真的向飞书发包，结果也不依赖网络环境。
        url = "https://192.0.2.1/open-apis/bot/v2/hook/" + self.TOKEN
        ok, why = ln.post(url, {"a": 1}, timeout=0.05, hosts=("192.0.2.1",))
        self.assertFalse(ok)
        self.assertNotIn(self.TOKEN, why, "网络异常文本可能带 URL，必须只保留错误类别")

    def test_mask_hides_token(self):
        masked = ln.mask("https://open.feishu.cn/open-apis/bot/v2/hook/" + self.TOKEN)
        self.assertNotIn(self.TOKEN, masked)
        self.assertIn("open.feishu.cn", masked)


class TestHookBudget(unittest.TestCase):
    """[medium] hook 总耗时必须守在 plugin.json 的硬超时之内。"""

    def test_budget_never_exceeds_remaining(self):
        b = ln.Budget(2.0)
        self.assertLessEqual(b.slice(30), 2.0)
        self.assertGreater(b.slice(30), 0)

    def test_exhausted_budget_still_returns_positive_slice(self):
        b = ln.Budget(0.5)
        b.deadline = 0  # 强制耗尽
        self.assertGreater(b.slice(5), 0, "切片必须为正，否则 subprocess/urllib 会报错")
        self.assertLessEqual(b.slice(5), 0.5)

    def test_total_budget_is_below_plugin_timeout(self):
        repo = Path(__file__).resolve().parent.parent
        manifest = json.loads((repo / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        hard = manifest["hooks"]["Stop"][0]["hooks"][0]["timeout"]
        self.assertLess(ln.HOOK_BUDGET_SECONDS, hard,
                        "内部预算必须明显小于 Claude Code 的硬超时")


class TestTranscriptSystemRecords(unittest.TestCase):
    """[medium] 压缩摘要与各种工具结果形态都不能被当成用户提问。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_has_tool_result_detects_content_blocks(self):
        self.assertTrue(ln.has_tool_result([{"type": "tool_result", "content": "x"}]))
        self.assertFalse(ln.has_tool_result([{"type": "text", "text": "x"}]))
        self.assertFalse(ln.has_tool_result("plain string"))
        self.assertFalse(ln.has_tool_result(None))

    def test_compact_summary_is_not_a_user_turn(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
             "userType": "external", "message": {"content": "真实提问"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:01.000Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:00:02.000Z", "userType": "external",
             "isCompactSummary": True,
             "message": {"content": "这是更早会话的压缩摘要，含敏感历史"}},
            {"type": "user", "timestamp": "2026-01-01T00:00:03.000Z", "userType": "external",
             "isVisibleInTranscriptOnly": True,
             "message": {"content": "仅在 transcript 可见的内容"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:09.000Z",
             "message": {"content": [{"type": "text", "text": "完成"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "真实提问")
        self.assertEqual(stats["tool_calls"], 1)

    def test_tool_result_block_without_top_level_field(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
             "userType": "external", "message": {"content": "真实提问"}},
            {"type": "user", "timestamp": "2026-01-01T00:00:01.000Z", "userType": "external",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                       "content": "命令输出"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:05.000Z",
             "message": {"content": [{"type": "text", "text": "好了"}]}},
        ])
        self.assertEqual(ln.parse_transcript(path)["user_prompt"], "真实提问")

    def test_huge_transcript_reads_tail_only(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        filler = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 900}]}}
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "ai-title", "aiTitle": "很早以前的标题"}) + "\n")
            line = json.dumps(filler, ensure_ascii=False) + "\n"
            for _ in range((ln.MAX_TRANSCRIPT_BYTES // len(line)) + 200):
                f.write(line)
            f.write(json.dumps({"type": "user", "userType": "external",
                                "timestamp": "2026-01-01T00:00:00.000Z",
                                "message": {"content": "尾部的真实提问"}}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:04.000Z",
                                "message": {"content": [{"type": "text", "text": "尾部回复"}]}},
                               ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        start = time.monotonic()
        stats = ln.parse_transcript(path)
        elapsed = time.monotonic() - start
        self.assertEqual(stats["user_prompt"], "尾部的真实提问")
        self.assertEqual(stats["assistant_text"], "尾部回复")
        self.assertLess(elapsed, 5.0, "超大 transcript 解析必须远快于 hook 预算")


# ── codex 第二轮对抗审查回归用例 ────────────────────────────────────

class TestUntrustedConfigReads(ConfigSandbox):
    """[high] 项目配置不得跟随符号链接或读取特殊文件。"""

    def test_symlink_project_config_is_refused(self):
        target = Path(self.tmp) / "real.json"
        target.write_text(json.dumps({"events": ["Stop"]}), encoding="utf-8")
        link = self.project / ".claude" / "lark.json"
        link.symlink_to(target)
        cfg = ln.load_config(str(self.project))
        self.assertTrue(any("符号链接" in w for w in cfg["_warnings"]))
        self.assertIn("_fatal", cfg, "拒绝读取后必须停发，而不是当作无配置继续")

    @unittest.skipUnless(os.path.exists("/dev/zero"), "需要 /dev/zero")
    def test_character_device_does_not_hang(self):
        """符号链接指向 /dev/zero 曾可让 hook 无限读取。"""
        link = self.project / ".claude" / "lark.json"
        link.symlink_to("/dev/zero")
        start = time.monotonic()
        cfg = ln.load_config(str(self.project))
        self.assertLess(time.monotonic() - start, 2.0, "必须立刻拒绝，不能无界读取")
        self.assertIn("_fatal", cfg)

    def test_oversized_config_is_refused(self):
        path = self.project / ".claude" / "lark.json"
        path.write_text("{\"events\": [" + '"Stop",' * 60000 + '"Stop"]}', encoding="utf-8")
        cfg = ln.load_config(str(self.project))
        self.assertTrue(any("超过" in w for w in cfg["_warnings"]), cfg["_warnings"])

    def test_directory_in_place_of_config(self):
        path = self.project / ".claude" / "lark.json"
        path.mkdir()
        cfg = ln.load_config(str(self.project))
        self.assertNotIn("webhook_url", cfg)


class TestProjectConfigIsMonotonic(ConfigSandbox):
    """[medium] 项目配置只能收窄，不能放宽用户设定。"""

    def test_cannot_reenable_summary(self):
        self.write_user({"webhook_url": SAFE_URL, "include_summary": False})
        self.write_project({"include_summary": True})
        cfg = ln.load_config(str(self.project))
        self.assertIs(cfg["include_summary"], False, "仓库不得重新打开会话摘要")

    def test_cannot_add_events(self):
        self.write_user({"webhook_url": SAFE_URL, "events": ["Stop"]})
        self.write_project({"events": ["Stop", "SubagentStop", "SessionEnd"]})
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["events"], ["Stop"], "events 只能取交集")

    def test_can_remove_events(self):
        self.write_user({"webhook_url": SAFE_URL, "events": ["Stop", "Notification"]})
        self.write_project({"events": ["Stop"]})
        self.assertEqual(ln.load_config(str(self.project))["events"], ["Stop"])

    def test_cannot_cancel_quiet_hours(self):
        self.write_user({"webhook_url": SAFE_URL, "quiet_hours": [22, 8]})
        self.write_project({"quiet_hours": None})
        self.assertEqual(ln.load_config(str(self.project))["quiet_hours"], [22, 8])

    def test_can_add_quiet_hours_when_user_has_none(self):
        self.write_user({"webhook_url": SAFE_URL})
        self.write_project({"quiet_hours": [1, 2]})
        self.assertEqual(ln.load_config(str(self.project))["quiet_hours"], [1, 2])

    def test_min_duration_takes_the_stricter_value(self):
        self.write_user({"webhook_url": SAFE_URL, "min_duration_seconds": 30})
        self.write_project({"min_duration_seconds": 0})
        self.assertEqual(ln.load_config(str(self.project))["min_duration_seconds"], 30)

    def test_debug_cannot_be_enabled_by_project(self):
        self.write_user({"webhook_url": SAFE_URL})
        self.write_project({"debug": True})
        self.assertFalse(ln.load_config(str(self.project)).get("debug"),
                         "仓库不得让会话内容开始落盘")


class TestResponseBodyNeverLeaks(unittest.TestCase):
    """[medium] 远端返回的任何文本都不得把 token 带进日志。"""

    URL = "https://open.feishu.cn/open-apis/bot/v2/hook/AAAA-BBBB-TOKEN123"

    def serve_once(self, status, body, content_type="application/json"):
        """起一个只应答一次的本地服务，返回它的 URL（路径里带 token）。

        同时临时放行目的地校验 —— 否则请求在 https/主机检查处就被拦下，
        这些用例会「通过」但根本没走到响应解析，等于白测。
        """
        import http.server, threading

        payload = body if isinstance(body, bytes) else body.encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        self.addCleanup(srv.server_close)

        real = ln.validate_webhook
        ln.validate_webhook = lambda url, hosts=(): (True, "")
        self.addCleanup(lambda: setattr(ln, "validate_webhook", real))

        return "http://127.0.0.1:%d/open-apis/bot/v2/hook/TOKEN123" % srv.server_address[1]

    def test_scrub_removes_full_url(self):
        out = ln.scrub_url("bad request for %s please retry" % self.URL, self.URL)
        self.assertNotIn("TOKEN123", out)
        self.assertIn("***", out)

    def test_scrub_removes_path_and_tail_token(self):
        for echo in ("/open-apis/bot/v2/hook/AAAA-BBBB-TOKEN123", "AAAA-BBBB-TOKEN123"):
            out = ln.scrub_url("server said: " + echo, self.URL)
            self.assertNotIn("TOKEN123", out, echo)

    def test_scrub_is_safe_on_empty(self):
        self.assertEqual(ln.scrub_url("", self.URL), "")
        self.assertEqual(ln.scrub_url("hi", ""), "hi")

    def test_http_error_body_is_omitted(self):
        url = self.serve_once(404, "<html>404 not found: /hook/TOKEN123</html>", "text/html")
        ok, why = ln.post(url, {"a": 1})
        self.assertFalse(ok)
        self.assertIn("404", why, "状态码要保留")
        self.assertNotIn("TOKEN123", why, "HTTP 错误正文会回显路径，必须省略")

    def test_non_json_response_body_is_omitted(self):
        url = self.serve_once(200, "<html>proxy error at /hook/TOKEN123</html>", "text/html")
        ok, why = ln.post(url, {"a": 1})
        self.assertFalse(ok)
        self.assertNotIn("TOKEN123", why)

    def test_business_error_msg_is_never_echoed(self):
        """远端 msg 一律不回显——脱敏挡不住任意编码变体，只能不记。"""
        body = json.dumps({"code": 19021,
                           "msg": "sign match fail /hook/AAAA%2DBBBB%2DTOKEN123"})
        url = self.serve_once(200, body)
        ok, why = ln.post(url, {"a": 1})
        self.assertFalse(ok)
        self.assertIn("19021", why, "业务错误码要保留，排错离不开它")
        self.assertIn("签名校验失败", why, "应给出我们自己的静态说明")
        self.assertNotIn("TOKEN123", why)
        self.assertNotIn("sign match fail", why, "远端文案不得出现")

    def test_unknown_code_is_reported_without_guessing(self):
        url = self.serve_once(200, json.dumps({"code": 12345, "msg": "whatever /TOKEN123"}))
        ok, why = ln.post(url, {"a": 1})
        self.assertFalse(ok)
        self.assertIn("12345", why)
        self.assertNotIn("TOKEN123", why)

    def test_success_is_reported(self):
        url = self.serve_once(200, json.dumps({"code": 0, "msg": "success"}))
        ok, why = ln.post(url, {"a": 1})
        self.assertTrue(ok, why)

    def test_oversized_response_is_capped(self):
        huge = json.dumps({"code": 0, "pad": "x" * (ln.MAX_RESPONSE_BYTES * 2)})
        url = self.serve_once(200, huge)
        start = time.monotonic()
        ok, why = ln.post(url, {"a": 1})
        self.assertLess(time.monotonic() - start, 10.0)
        # 被截断后不再是合法 JSON，但必须是「省略正文」的失败，而不是泄漏
        self.assertNotIn("xxxxxxxxxx", why)


class TestWatchdogIsRealWallClock(unittest.TestCase):
    """[medium] 预算必须能打断 stdin/文件读取这类内部阻塞。"""

    @unittest.skipUnless(hasattr(signal, "SIGALRM"), "需要 SIGALRM")
    def test_watchdog_interrupts_a_blocking_read(self):
        r, w = os.pipe()          # 写端保持打开：read() 会一直等 EOF
        self.addCleanup(os.close, w)
        old = ln.install_watchdog(0.5)
        start = time.monotonic()
        try:
            with self.assertRaises(ln.HookTimeout):
                with os.fdopen(r, "r") as f:
                    f.read()
        finally:
            ln.cancel_watchdog(old)
        self.assertLess(time.monotonic() - start, 3.0, "看门狗必须真的打断阻塞读")

    @unittest.skipUnless(hasattr(signal, "SIGALRM"), "需要 SIGALRM")
    def test_cancel_watchdog_stops_the_alarm(self):
        old = ln.install_watchdog(0.3)
        ln.cancel_watchdog(old)
        time.sleep(0.6)   # 若定时器未取消，这里会抛 HookTimeout

    def test_hook_with_blocking_stdin_exits_quickly(self):
        """端到端：写入事件后不关闭管道，hook 必须自己收场。"""
        repo = Path(__file__).resolve().parent.parent
        script = repo / "scripts" / "lark_notify.py"
        env = dict(os.environ)
        env.pop("LARK_WEBHOOK_URL", None)
        env.pop("FEISHU_WEBHOOK_URL", None)
        env["CLAUDE_HOOK_LARK_CONFIG"] = str(Path(self.id_dir()) / "none.json")
        proc = subprocess.Popen(
            [sys.executable, str(script), "hook", "--dry-run"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        proc.stdin.write(b'{"hook_event_name":"Stop"}')
        proc.stdin.flush()        # 故意不 close，模拟父进程不关管道
        start = time.monotonic()
        try:
            proc.wait(timeout=ln.HOOK_BUDGET_SECONDS + 6)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("hook 未在预算内退出，会撞上 plugin.json 的硬超时")
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
        self.assertEqual(proc.returncode, 0, "hook 必须始终以 0 退出")
        self.assertLess(time.monotonic() - start, ln.HOOK_BUDGET_SECONDS + 5)

    def id_dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return d


class TestTurnBoundaries(unittest.TestCase):
    """[medium] 无文本的真实用户轮次也必须重置，避免拼出「上一轮任务 + 本轮回答」。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_attachment_only_message_resets_the_turn(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "上一轮的敏感任务：导出生产库凭据"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:02.000Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:03.000Z",
             "message": {"content": [{"type": "text", "text": "上一轮回答"}]}},
            # 纯图片消息：是真实用户输入，但没有 text block
            {"type": "user", "timestamp": "2026-01-01T00:00:10.000Z", "userType": "external",
             "message": {"content": [{"type": "image",
                                       "source": {"type": "base64", "data": "iVBOR"}}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:20.000Z",
             "message": {"content": [{"type": "text", "text": "这张图是架构图"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "",
                         "无文本输入应留空，而不是沿用上一轮任务")
        self.assertNotIn("敏感", stats["user_prompt"])
        self.assertEqual(stats["assistant_text"], "这张图是架构图")
        self.assertEqual(stats["tool_calls"], 0, "轮次已重置，工具计数应归零")
        self.assertAlmostEqual(ln.turn_duration(stats), 10.0, places=1)

    def test_card_omits_task_line_when_prompt_is_empty(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "上一轮的敏感任务"}},
            {"type": "user", "timestamp": "2026-01-01T00:00:10.000Z", "userType": "external",
             "message": {"content": [{"type": "image", "source": {"data": "x"}}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:12.000Z",
             "message": {"content": [{"type": "text", "text": "看到了"}]}},
        ])
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {})
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("敏感", blob, "上一轮任务绝不能被本轮卡片带出去")
        self.assertIn("看到了", blob)

    def test_slash_command_clears_the_previous_task(self):
        """斜杠命令算轮次边界且不带任务文本。

        /model 这类纯控制命令和 /review 这类会真正驱动一轮工作的命令，
        单看记录无法区分。猜错的代价不对称：当成非边界会把上一轮任务原文
        再发一次；当成边界只是少显示一行。所以一律按边界处理。
        """
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "上一轮的敏感任务：导出生产库凭据"}},
            {"type": "user", "timestamp": "2026-01-01T00:00:05.000Z", "userType": "external",
             "message": {"content": "<command-name>/model</command-name>"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:30.000Z",
             "message": {"content": [{"type": "text", "text": "已切换"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "")

    def test_standalone_slash_task_does_not_leak_previous_prompt(self):
        """独立的 /review：命令记录 + isMeta 展开 + 助手工作，绝不能带出上一轮任务。"""
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "上一轮的敏感任务：导出生产库凭据"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:03.000Z",
             "message": {"content": [{"type": "text", "text": "上一轮回答"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:01:00.000Z", "userType": "external",
             "message": {"content": "<command-name>/review</command-name>"}},
            {"type": "user", "timestamp": "2026-01-01T00:01:01.000Z", "userType": "external",
             "isMeta": True, "message": {"content": [{"type": "text", "text": "Run a review..."}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:01:40.000Z",
             "message": {"content": [{"type": "text", "text": "审查完成"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "")
        self.assertEqual(stats["assistant_text"], "审查完成")
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {})
        self.assertNotIn("敏感", json.dumps(card, ensure_ascii=False))

    def test_system_injections_never_show_as_the_task(self):
        """task-notification 等注入记录没有 isMeta，曾被当成本轮任务显示。"""
        for tag in ("<task-notification>\n<task-id>abc</task-id>\n</task-notification>",
                    "<local-command-caveat>Caveat: ...</local-command-caveat>",
                    "<user-prompt-submit-hook>hook output</user-prompt-submit-hook>"):
            kind, text = ln.classify_user_text(tag)
            self.assertEqual(kind, "tagged", tag[:30])
            self.assertEqual(text, "", "标签文本本身不得展示")

    def test_background_task_notification_does_not_replace_the_task(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "把飞书插件写完"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:02.000Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:05:00.000Z", "userType": "external",
             "message": {"content": "<task-notification>\n<task-id>xyz</task-id>\n"
                                     "<status>completed</status>\n</task-notification>"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:06:00.000Z",
             "message": {"content": [{"type": "text", "text": "后台任务回来了，继续"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "", "隐私边界：不保留上一轮任务")
        self.assertNotIn("task-notification", stats["user_prompt"])
        self.assertEqual(stats["tool_calls"], 0, "完整边界：统计也从头算")
        self.assertAlmostEqual(ln.turn_duration(stats), 60.0, places=1)

    def test_classify_user_text(self):
        self.assertEqual(ln.classify_user_text("<local-command-stdout>x</local-command-stdout>"),
                         ("tagged", ""))
        self.assertEqual(ln.classify_user_text("<command-name>/clear</command-name>"),
                         ("tagged", ""))
        self.assertEqual(ln.classify_user_text("正常提问"), ("prompt", "正常提问"))
        self.assertEqual(ln.classify_user_text(""), ("prompt", ""),
                         "纯图片/附件消息：是新一轮，但没有文本")


# ── codex 第三轮对抗审查回归用例 ────────────────────────────────────

class TestWatchdogCannotBeSwallowed(unittest.TestCase):
    """[medium] HookTimeout 曾继承 Exception，被 stdin 处的兜底 except 吞掉后照发不误。"""

    def test_hook_timeout_is_not_an_exception_subclass(self):
        self.assertTrue(issubclass(ln.HookTimeout, BaseException))
        self.assertFalse(issubclass(ln.HookTimeout, Exception),
                         "继承 Exception 会被本文件里大量的 except Exception 吞掉")

    def test_broad_except_does_not_catch_it(self):
        caught = None
        try:
            try:
                raise ln.HookTimeout()
            except Exception:
                caught = "swallowed"
        except ln.HookTimeout:
            caught = "propagated"
        self.assertEqual(caught, "propagated")

    def test_timeout_during_stdin_sends_nothing(self):
        """端到端：stdin 永不关闭时，post 必须一次都没被调用。"""
        repo = Path(__file__).resolve().parent.parent
        probe = repo / "tests" / "_probe_no_send.py"
        probe.write_text(
            "import sys, os\n"
            "sys.path.insert(0, %r)\n" % str(repo / "scripts") +
            "import lark_notify as ln\n"
            "ln.HOOK_BUDGET_SECONDS = 1.0\n"
            "calls = []\n"
            "ln.post = lambda *a, **k: (calls.append(1), (True, 'ok'))[1]\n"
            "ln.build_hook_payload = lambda *a, **k: (_ for _ in ()).throw("
            "AssertionError('超时后不应再构卡'))\n"
            "rc = ln.main(['hook'])\n"
            "print('rc=%s calls=%d' % (rc, len(calls)))\n",
            encoding="utf-8")
        self.addCleanup(probe.unlink, True)
        proc = subprocess.Popen([sys.executable, str(probe)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        proc.stdin.write('{"hook_event_name":"Stop"}')
        proc.stdin.flush()
        # 关键：绝不能用 communicate()，它会关闭 stdin 制造 EOF，
        # 于是什么都不会阻塞，这个用例就变成了空测。
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("超时后进程未自行退出")
        out = proc.stdout.read()
        err = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        proc.stdin.close()
        self.assertIn("calls=0", out, "看门狗触发后绝不能再发送。stdout=%r stderr=%r" % (out, err))
        self.assertIn("rc=0", out)


class TestRemoteCodeIsNeverEchoed(unittest.TestCase):
    """[medium] 远端可把 token 塞进 code 字段绕过「正文不回显」。"""

    def test_normalize_rejects_non_numeric(self):
        for bad in ("https://open.feishu.cn/hook/TOKEN123", None, [], {}, True, False,
                    "1e5", "0x10", "9" * 40):
            self.assertIsNone(ln._normalize_code(bad), repr(bad))

    def test_normalize_accepts_plain_integers(self):
        self.assertEqual(ln._normalize_code(19021), 19021)
        self.assertEqual(ln._normalize_code("19021"), 19021)
        self.assertEqual(ln._normalize_code(-1), -1)

    def test_url_in_code_field_is_not_logged(self):
        import http.server, threading
        body = json.dumps({"code": "https://open.feishu.cn/hook/TOKEN123"})

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                p = body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(p)))
                self.end_headers()
                self.wfile.write(p)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        self.addCleanup(srv.server_close)
        real = ln.validate_webhook
        ln.validate_webhook = lambda url, hosts=(): (True, "")
        self.addCleanup(lambda: setattr(ln, "validate_webhook", real))
        ok, why = ln.post("http://127.0.0.1:%d/h" % srv.server_address[1], {"a": 1})
        self.assertFalse(ok)
        self.assertNotIn("TOKEN123", why)
        self.assertNotIn("open.feishu.cn", why)


class TestEmptyEventsMeansDenyAll(ConfigSandbox):
    """[medium] 空 events 曾被 `or DEFAULT_EVENTS` 解释成默认值，反而放宽。"""

    def test_explicit_empty_list_blocks_everything(self):
        for event in ("Stop", "Notification", "SubagentStop", "SessionEnd"):
            ok, reason = ln.should_notify({"hook_event_name": event}, {"events": []})
            self.assertFalse(ok, "%s 不该通过" % event)

    def test_disjoint_intersection_blocks_everything(self):
        self.write_user({"webhook_url": SAFE_URL, "events": ["Notification"]})
        self.write_project({"events": ["Stop"]})
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["events"], [], "交集为空")
        for event in ("Stop", "Notification"):
            self.assertFalse(ln.should_notify({"hook_event_name": event}, cfg)[0],
                             "%s 在空交集下必须被拒绝" % event)

    def test_missing_key_still_uses_defaults(self):
        self.assertTrue(ln.should_notify({"hook_event_name": "Stop"}, {})[0])


class TestExplicitConfigFailureStops(ConfigSandbox):
    """[medium] CLAUDE_HOOK_LARK_CONFIG 损坏时曾静默回落到用户级收件群。"""

    def test_broken_explicit_config_does_not_fall_back(self):
        self.write_user({"webhook_url": SAFE_URL})
        broken = Path(self.tmp) / "broken.json"
        broken.write_text("{ nope", encoding="utf-8")
        os.environ["CLAUDE_HOOK_LARK_CONFIG"] = str(broken)
        self.addCleanup(os.environ.pop, "CLAUDE_HOOK_LARK_CONFIG", None)
        cfg = ln.load_config(str(self.project))
        self.assertIn("_fatal", cfg, "必须停发，而不是退回用户级群")

    def test_missing_explicit_config_does_not_fall_back(self):
        self.write_user({"webhook_url": SAFE_URL})
        os.environ["CLAUDE_HOOK_LARK_CONFIG"] = str(Path(self.tmp) / "gone.json")
        self.addCleanup(os.environ.pop, "CLAUDE_HOOK_LARK_CONFIG", None)
        cfg = ln.load_config(str(self.project))
        self.assertIn("_fatal", cfg)

    def test_valid_explicit_config_overrides(self):
        self.write_user({"webhook_url": SAFE_URL})
        good = Path(self.tmp) / "good.json"
        good.write_text(json.dumps({"webhook_url": SAFE_URL + "-explicit"}), encoding="utf-8")
        os.environ["CLAUDE_HOOK_LARK_CONFIG"] = str(good)
        self.addCleanup(os.environ.pop, "CLAUDE_HOOK_LARK_CONFIG", None)
        cfg = ln.load_config(str(self.project))
        self.assertNotIn("_fatal", cfg)
        self.assertEqual(cfg["webhook_url"], SAFE_URL + "-explicit")


# ── codex 第四轮对抗审查回归用例 ────────────────────────────────────

class TestForgedReservedTags(unittest.TestCase):
    """[high] 真人可以手打保留标签，不能靠它保留上一轮任务。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_every_output_prefix_clears_previous_prompt(self):
        for tag in ln.OUTPUT_PREFIXES:
            path = self._write([
                {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
                 "message": {"content": "上一轮敏感任务：导出生产库凭据"}},
                {"type": "assistant", "timestamp": "2026-01-01T00:00:02.000Z",
                 "message": {"content": [{"type": "text", "text": "上一轮回答"}]}},
                # 真人手打一段以保留标签开头的文本，请 Claude 分析
                {"type": "user", "timestamp": "2026-01-01T00:01:00.000Z", "userType": "external",
                 "message": {"content": tag + "帮我看看这段是什么意思"}},
                {"type": "assistant", "timestamp": "2026-01-01T00:01:30.000Z",
                 "message": {"content": [{"type": "text", "text": "本轮回答"}]}},
            ])
            stats = ln.parse_transcript(path)
            self.assertEqual(stats["user_prompt"], "", "%s 不得保留上一轮任务" % tag)
            card = ln.build_hook_payload(
                {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {})
            blob = json.dumps(card, ensure_ascii=False)
            self.assertNotIn("敏感", blob, "%s 泄漏了上一轮任务" % tag)
            self.assertNotIn(tag.strip("<>"), blob, "%s 标签文本不应展示" % tag)


class TestExplicitConfigTakesOverDestination(ConfigSandbox):
    """[high] 显式配置生效时不得继承用户级目的地。"""

    def _use_explicit(self, data):
        path = Path(self.tmp) / "explicit.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.environ["CLAUDE_HOOK_LARK_CONFIG"] = str(path)
        self.addCleanup(os.environ.pop, "CLAUDE_HOOK_LARK_CONFIG", None)

    def test_empty_explicit_config_does_not_inherit_url(self):
        self.write_user({"webhook_url": SAFE_URL, "secret": "s"})
        self._use_explicit({})
        cfg = ln.load_config(str(self.project))
        self.assertNotIn("webhook_url", cfg, "{} 不得沿用用户级收件群")
        self.assertNotIn("secret", cfg)

    def test_behaviour_only_explicit_config_does_not_inherit_url(self):
        self.write_user({"webhook_url": SAFE_URL, "secret": "s"})
        self._use_explicit({"events": ["Stop"], "timeout": 3})
        cfg = ln.load_config(str(self.project))
        self.assertNotIn("webhook_url", cfg)
        self.assertEqual(cfg["events"], ["Stop"])

    def test_explicit_registry_replaces_user_registry(self):
        self.write_user({"webhook_url": SAFE_URL,
                         "webhooks": {"群": {"webhook_url": SAFE_URL + "-user"}}})
        self._use_explicit({"webhook": "群"})
        cfg = ln.load_config(str(self.project))
        self.assertIn("_fatal", cfg, "用户级具名表不应被显式配置继承")

    def test_env_still_overrides_explicit(self):
        self.write_user({"webhook_url": SAFE_URL})
        self._use_explicit({})
        os.environ["LARK_WEBHOOK_URL"] = SAFE_URL + "-env"
        self.addCleanup(os.environ.pop, "LARK_WEBHOOK_URL", None)
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["webhook_url"], SAFE_URL + "-env")


class TestSuccessDetection(unittest.TestCase):
    """[medium] False == 0、0.0 == 0，直接比较会把畸形响应当成发送成功。"""

    def serve(self, body):
        import http.server, threading
        payload = json.dumps(body).encode() if not isinstance(body, bytes) else body

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        self.addCleanup(srv.server_close)
        real = ln.validate_webhook
        ln.validate_webhook = lambda url, hosts=(): (True, "")
        self.addCleanup(lambda: setattr(ln, "validate_webhook", real))
        return "http://127.0.0.1:%d/h" % srv.server_address[1]

    def test_real_success_still_works(self):
        ok, why = ln.post(self.serve({"code": 0, "msg": "success"}), {"a": 1})
        self.assertTrue(ok, why)

    def test_status_code_variant_works(self):
        ok, why = ln.post(self.serve({"StatusCode": 0}), {"a": 1})
        self.assertTrue(ok, why)

    def test_false_is_not_success(self):
        ok, why = ln.post(self.serve({"code": False}), {"a": 1})
        self.assertFalse(ok, "False == 0 不能算成功")

    def test_null_is_not_success(self):
        self.assertFalse(ln.post(self.serve({"code": None}), {"a": 1})[0])

    def test_float_zero_is_not_success(self):
        self.assertFalse(ln.post(self.serve({"code": 0.0}), {"a": 1})[0])

    def test_missing_code_is_not_success(self):
        self.assertFalse(ln.post(self.serve({"msg": "ok"}), {"a": 1})[0])

    def test_string_zero_is_success(self):
        self.assertTrue(ln.post(self.serve({"code": "0"}), {"a": 1})[0])


class TestEventsTypeIsFailClosed(ConfigSandbox):
    """[medium] events 类型非法曾回落到默认值，等于放宽。"""

    def test_missing_key_uses_defaults(self):
        self.assertEqual(ln.effective_events({}), list(ln.DEFAULT_EVENTS))

    def test_illegal_types_deny_everything(self):
        for bad in (None, "Stop", 5, True, {"Stop": 1}):
            self.assertEqual(ln.effective_events({"events": bad}), [], repr(bad))
            self.assertFalse(ln.should_notify({"hook_event_name": "Stop"}, {"events": bad})[0])

    def test_string_events_do_not_enable_notification(self):
        """常见写法 "events": "Stop" 本想只要 Stop，绝不能反而多发 Notification。"""
        self.assertFalse(ln.should_notify({"hook_event_name": "Notification"},
                                          {"events": "Stop"})[0])

    def test_illegal_type_is_surfaced_to_the_user(self):
        self.write_user({"webhook_url": SAFE_URL, "events": "Stop"})
        cfg = ln.load_config(str(self.project))
        self.assertTrue(any("events" in w for w in cfg["_warnings"]), cfg["_warnings"])


# ── codex 第五轮对抗审查回归用例 ────────────────────────────────────

class TestBoundaryIsComplete(unittest.TestCase):
    """[high] 只清任务不够：上一轮回答常复述任务内容，统计也会跨轮。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_previous_answer_is_not_carried_across_a_tag_boundary(self):
        """本轮只有 tool_use、没有新文本回答时，绝不能拿上一轮回答顶上。"""
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "导出生产库凭据"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:05.000Z",
             "message": {"content": [{"type": "text",
                                       "text": "好的，我来导出生产库凭据：user=root pass=hunter2"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:01:00.000Z", "userType": "external",
             "message": {"content": "<task-notification>后台任务完成</task-notification>"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:01:10.000Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "")
        self.assertEqual(stats["assistant_text"], "", "上一轮回答不得跨边界保留")
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {})
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("生产库凭据", blob)

    def test_duration_does_not_span_the_boundary(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "上一轮"}},
            {"type": "user", "timestamp": "2026-01-01T01:00:00.000Z", "userType": "external",
             "message": {"content": "<task-notification>x</task-notification>"}},
            {"type": "assistant", "timestamp": "2026-01-01T01:00:03.000Z",
             "message": {"content": [{"type": "text", "text": "好了"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertAlmostEqual(ln.turn_duration(stats), 3.0, places=1,
                               msg="耗时必须从边界重新计，不能横跨一小时")

    def test_short_turn_cannot_bypass_min_duration(self):
        """跨轮 turn_start 会把 3 秒的活儿算成一小时，绕过最短耗时过滤。"""
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "上一轮"}},
            {"type": "user", "timestamp": "2026-01-01T01:00:00.000Z", "userType": "external",
             "message": {"content": "<task-notification>x</task-notification>"}},
            {"type": "assistant", "timestamp": "2026-01-01T01:00:03.000Z",
             "message": {"content": [{"type": "text", "text": "好了"}]}},
        ])
        stats = ln.parse_transcript(path)
        ok, reason = ln.should_notify(
            {"hook_event_name": "Stop", "transcript_path": path},
            {"events": ["Stop"], "min_duration_seconds": 60}, stats)
        self.assertFalse(ok, "3 秒的一轮不该通过 60 秒门槛")
        self.assertIn("min_duration", reason)


class TestExplicitConfigDropsSelector(ConfigSandbox):
    """[high] 显式配置接管时，继承来的具名选择器也必须清掉。"""

    def _use_explicit(self, data):
        path = Path(self.tmp) / "explicit.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.environ["CLAUDE_HOOK_LARK_CONFIG"] = str(path)
        self.addCleanup(os.environ.pop, "CLAUDE_HOOK_LARK_CONFIG", None)

    def test_inherited_selector_does_not_pick_explicit_registry_entry(self):
        self.write_user({"webhook": "prod",
                         "webhooks": {"prod": {"webhook_url": SAFE_URL + "-user-prod"}}})
        self._use_explicit({
            "webhook_url": SAFE_URL + "-explicit-direct",
            "webhooks": {"prod": {"webhook_url": SAFE_URL + "-explicit-prod"}},
        })
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["webhook_url"], SAFE_URL + "-explicit-direct",
                         "显式配置没选名字，就该用它自己的直接地址")

    def test_explicit_selector_still_works(self):
        self.write_user({"webhook_url": SAFE_URL})
        self._use_explicit({
            "webhook": "prod",
            "webhooks": {"prod": {"webhook_url": SAFE_URL + "-explicit-prod", "secret": "e"}},
        })
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["webhook_url"], SAFE_URL + "-explicit-prod")
        self.assertEqual(cfg["secret"], "e")


class TestProjectIllegalEventsDenies(ConfigSandbox):
    """[medium] 项目级非法 events 被忽略后会回落到默认，等于放宽。"""

    def test_illegal_project_events_denies_all_even_without_upper_events(self):
        self.write_user({"webhook_url": SAFE_URL})       # 上层刻意不配 events
        self.write_project({"events": "Stop"})
        cfg = ln.load_config(str(self.project))
        self.assertEqual(ln.effective_events(cfg), [],
                         "非法类型必须拒绝全部，而不是回落到默认的 Stop+Notification")
        self.assertFalse(ln.should_notify({"hook_event_name": "Notification"}, cfg)[0])
        self.assertTrue(any("events" in w for w in cfg["_warnings"]), cfg["_warnings"])

    def test_legal_project_events_still_intersect(self):
        self.write_user({"webhook_url": SAFE_URL})
        self.write_project({"events": ["Stop"]})
        self.assertEqual(ln.effective_events(ln.load_config(str(self.project))), ["Stop"])


# ── codex 第六轮对抗审查回归用例 ────────────────────────────────────

class TestCorruptLinesFailClosed(unittest.TestCase):
    """[high] 中间的坏行可能藏着一条轮次边界；末尾的半截行则是常态。"""

    def _write_raw(self, lines, terminate_last=True):
        """terminate_last=False 表示最后一行没有换行符 —— 也就是「正在追加、写了一半」。

        这个区别是本组用例的全部要点：以换行结尾却解析失败的行，是**已经写完的坏记录**；
        没有换行的尾行才是半截。先 strip 再判断的话两者无法区分。
        """
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for i, l in enumerate(lines):
                last = i == len(lines) - 1
                f.write(l if (last and not terminate_last) else l + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_interior_corrupt_line_clears_previous_turn(self):
        path = self._write_raw([
            json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
                        "userType": "external",
                        "message": {"content": "导出生产库凭据"}}, ensure_ascii=False),
            json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:05.000Z",
                        "message": {"content": [{"type": "text",
                                                  "text": "凭据是 pass=hunter2"}]}},
                       ensure_ascii=False),
            '{"type": "user", "message": {"content": "本轮真实提问',   # 截断的边界行
            json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:01:00.000Z",
                        "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}),
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "", "坏行可能就是边界，必须 fail closed")
        self.assertEqual(stats["assistant_text"], "")
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {})
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("生产库凭据", blob)

    def test_trailing_partial_line_is_tolerated(self):
        """Stop 触发时文件常在追加写，最后一行是半截的——不能因此清空卡片。"""
        path = self._write_raw([
            json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
                        "userType": "external", "message": {"content": "本轮任务"}},
                       ensure_ascii=False),
            json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:20.000Z",
                        "message": {"content": [{"type": "text", "text": "本轮回答"}]}},
                       ensure_ascii=False),
            '{"type": "assistant", "message": {"content": [{"type": "te',   # 半截尾行
        ], terminate_last=False)
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "本轮任务")
        self.assertEqual(stats["assistant_text"], "本轮回答")

    def test_terminated_corrupt_tail_line_fails_closed(self):
        """以换行结尾的坏记录 = 已完整落盘的坏行，不是半截，必须 fail closed。"""
        path = self._write_raw([
            json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
                        "userType": "external", "message": {"content": "上一轮敏感任务"}},
                       ensure_ascii=False),
            json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:05.000Z",
                        "message": {"content": [{"type": "text", "text": "敏感回答"}]}},
                       ensure_ascii=False),
            '{"type": "user", "message": {"content": "被截断的边界',
        ], terminate_last=True)
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "")
        self.assertEqual(stats["assistant_text"], "")

    def test_two_consecutive_corrupt_lines_fail_closed(self):
        """追加写被打断只会留下一条半截尾行；连续两条说明是真损坏。"""
        path = self._write_raw([
            json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
                        "userType": "external", "message": {"content": "上一轮敏感任务"}},
                       ensure_ascii=False),
            '{"type": "user", "message": {"content": "截断一',
            '{"type": "user", "message": {"content": "截断二',
        ])
        self.assertEqual(ln.parse_transcript(path)["user_prompt"], "")

    def test_corrupt_line_then_sidechain_still_fails_closed(self):
        """sidechain 的 continue 必须排在 pending_corrupt 处理之后。"""
        path = self._write_raw([
            json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
                        "userType": "external", "message": {"content": "上一轮敏感任务"}},
                       ensure_ascii=False),
            '{"type": "user", "message": {"content": "截断',
            json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:02:00.000Z",
                        "isSidechain": True,
                        "message": {"content": [{"type": "text", "text": "subagent"}]}},
                       ensure_ascii=False),
        ])
        self.assertEqual(ln.parse_transcript(path)["user_prompt"], "")

    def test_single_trailing_corrupt_line_after_blank_is_tolerated(self):
        path = self._write_raw([
            json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
                        "userType": "external", "message": {"content": "本轮任务"}},
                       ensure_ascii=False),
            '{"type": "assistant", "message": {"content": [{"type": "te',
        ], terminate_last=False)
        self.assertEqual(ln.parse_transcript(path)["user_prompt"], "本轮任务")

    def test_non_object_line_also_fails_closed(self):
        path = self._write_raw([
            json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
                        "userType": "external", "message": {"content": "上一轮敏感任务"}},
                       ensure_ascii=False),
            "[1, 2, 3]",
            json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:01:00.000Z",
                        "message": {"content": [{"type": "text", "text": "本轮回答"}]}},
                       ensure_ascii=False),
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "")


class TestEmptyTaskPlaceholder(unittest.TestCase):
    """没有任务文本时给中性说明，但绝不泄漏内容。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_tagged_boundary_shows_neutral_placeholder(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "上一轮敏感任务"}},
            {"type": "user", "timestamp": "2026-01-01T00:01:00.000Z", "userType": "external",
             "message": {"content": "<task-notification>done</task-notification>"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:01:05.000Z",
             "message": {"content": [{"type": "text", "text": "继续"}]}},
        ])
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {})
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("由命令或系统事件触发", blob)
        self.assertNotIn("敏感", blob)
        self.assertNotIn("task-notification", blob)

    def test_attachment_only_turn_says_so(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": [{"type": "image", "source": {"data": "x"}}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:05.000Z",
             "message": {"content": [{"type": "text", "text": "看到了"}]}},
        ])
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {})
        self.assertIn("图片或附件", json.dumps(card, ensure_ascii=False))

    def test_placeholder_respects_include_summary_false(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "<task-notification>x</task-notification>"}},
        ])
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path},
            {"include_summary": False})
        self.assertNotIn("由命令或系统事件触发", json.dumps(card, ensure_ascii=False))


class TestSidechainIsOutOfScope(unittest.TestCase):
    """[high] subagent 记录曾污染父轮的回答、工具计数与耗时。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_sidechain_answer_does_not_override_parent(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "父轮任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:10.000Z",
             "message": {"content": [{"type": "text", "text": "父轮回答"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:20.000Z", "isSidechain": True,
             "message": {"content": [{"type": "text", "text": "subagent 的内部结论"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["assistant_text"], "父轮回答")
        self.assertNotIn("subagent", stats["assistant_text"])

    def test_sidechain_tools_are_not_counted_in_parent(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "父轮任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:05.000Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        ] + [
            {"type": "assistant", "timestamp": "2026-01-01T00:00:06.000Z", "isSidechain": True,
             "message": {"content": [{"type": "tool_use", "name": "Read"}]}}
            for _ in range(9)
        ])
        self.assertEqual(ln.parse_transcript(path)["tool_calls"], 1)

    def test_late_sidechain_cannot_inflate_duration(self):
        """晚到的 subagent 记录曾把 2 秒的父轮拉长到一小时，绕过最短耗时过滤。"""
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "父轮任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:02.000Z",
             "message": {"content": [{"type": "text", "text": "父轮回答"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T01:00:00.000Z", "isSidechain": True,
             "message": {"content": [{"type": "text", "text": "很久之后的 subagent 记录"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertAlmostEqual(ln.turn_duration(stats), 2.0, places=1)
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": 60}, stats)
        self.assertFalse(ok, "2 秒的父轮不该通过 60 秒门槛")


class TestTimeoutMergeIsMonotonic(ConfigSandbox):
    """[medium] `or 5` 把显式 0 当成缺失，项目配置反而放宽了超时。"""

    def test_zero_upper_timeout_is_not_widened(self):
        self.write_user({"webhook_url": SAFE_URL, "timeout": 0})
        self.write_project({"timeout": 10})
        cfg = ln.load_config(str(self.project))
        self.assertEqual(cfg["timeout"], 0, "min(0, 10) 应是 0，不是 5")

    def test_project_can_still_shorten(self):
        self.write_user({"webhook_url": SAFE_URL, "timeout": 8})
        self.write_project({"timeout": 2})
        self.assertEqual(ln.load_config(str(self.project))["timeout"], 2)

    def test_project_cannot_lengthen(self):
        self.write_user({"webhook_url": SAFE_URL, "timeout": 2})
        self.write_project({"timeout": 30})
        self.assertEqual(ln.load_config(str(self.project))["timeout"], 2)

    def test_missing_upper_timeout_uses_default(self):
        self.write_user({"webhook_url": SAFE_URL})
        self.write_project({"timeout": 30})
        self.assertEqual(ln.load_config(str(self.project))["timeout"], 5.0)


# ── codex 第七轮对抗审查回归用例 ────────────────────────────────────

class TestIncludeSummaryIsStrictBoolean(ConfigSandbox):
    """[high] "false" 字符串在 Python 里是真值，用户以为关了摘要其实没关。"""

    def test_string_false_does_not_enable_summary(self):
        self.write_user({"webhook_url": SAFE_URL, "include_summary": "false"})
        cfg = ln.load_config(str(self.project))
        self.assertFalse(ln.summary_enabled(cfg))
        self.assertTrue(any("include_summary" in w for w in cfg["_warnings"]), cfg["_warnings"])

    def test_all_illegal_types_fail_closed(self):
        for bad in ("false", "true", 0, 1, None, [], {}, "yes"):
            self.assertFalse(ln.summary_enabled({"include_summary": bad}), repr(bad))

    def test_missing_key_defaults_to_enabled(self):
        self.assertTrue(ln.summary_enabled({}))

    def test_real_true_still_works(self):
        self.assertTrue(ln.summary_enabled({"include_summary": True}))

    def test_illegal_value_keeps_prompt_out_of_the_card(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
                                "userType": "external",
                                "message": {"content": "密钥是 hunter2"}},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:05.000Z",
                                "message": {"content": [{"type": "text",
                                                          "text": "收到，hunter2 已记录"}]}},
                               ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path},
            {"include_summary": "false"})
        self.assertNotIn("hunter2", json.dumps(card, ensure_ascii=False))

    def test_project_string_false_also_fails_closed(self):
        self.write_user({"webhook_url": SAFE_URL, "include_summary": True})
        self.write_project({"include_summary": "false"})
        self.assertFalse(ln.summary_enabled(ln.load_config(str(self.project))))

    def test_project_cannot_reenable_with_string_true(self):
        self.write_user({"webhook_url": SAFE_URL, "include_summary": False})
        self.write_project({"include_summary": "true"})
        self.assertFalse(ln.summary_enabled(ln.load_config(str(self.project))))


class TestSubagentStopScope(unittest.TestCase):
    """[medium] SubagentStop 要读 agent 自己的 transcript，且只看 sidechain。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_picks_agent_transcript_path(self):
        path, sidechain = ln.transcript_for({
            "hook_event_name": "SubagentStop",
            "transcript_path": "/parent.jsonl",
            "agent_transcript_path": "/agent.jsonl"})
        self.assertEqual(path, "/agent.jsonl")
        self.assertTrue(sidechain)

    def test_stop_uses_main_chain(self):
        path, sidechain = ln.transcript_for({
            "hook_event_name": "Stop", "transcript_path": "/parent.jsonl"})
        self.assertEqual(path, "/parent.jsonl")
        self.assertFalse(sidechain)

    def test_subagent_without_agent_path_still_uses_sidechain_scope(self):
        _, sidechain = ln.transcript_for({
            "hook_event_name": "SubagentStop", "transcript_path": "/parent.jsonl"})
        self.assertTrue(sidechain)

    def test_subagent_card_reports_subtask_not_parent(self):
        agent = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "isSidechain": True, "message": {"content": "子任务：统计依赖数量"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:04.000Z", "isSidechain": True,
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:30.000Z", "isSidechain": True,
             "message": {"content": [{"type": "text", "text": "共 42 个依赖"}]}},
        ])
        card = ln.build_hook_payload(
            {"hook_event_name": "SubagentStop", "cwd": os.getcwd(),
             "transcript_path": "/parent.jsonl", "agent_transcript_path": agent}, {})
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("子任务：统计依赖数量", blob)
        self.assertIn("共 42 个依赖", blob)
        self.assertIn("子任务完成", blob)

    def test_subagent_duration_enables_min_duration_filter(self):
        agent = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "isSidechain": True, "message": {"content": "很快的子任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:03.000Z", "isSidechain": True,
             "message": {"content": [{"type": "text", "text": "好了"}]}},
        ])
        payload = {"hook_event_name": "SubagentStop", "transcript_path": "/parent.jsonl",
                   "agent_transcript_path": agent}
        stats = ln.parse_transcript(*ln.transcript_for(payload))
        self.assertAlmostEqual(ln.turn_duration(stats), 3.0, places=1)
        ok, _ = ln.should_notify(payload, {"events": ["SubagentStop"],
                                           "min_duration_seconds": 60}, stats)
        self.assertFalse(ok, "子任务耗时缺失曾让 min_duration 完全失效")

    def test_main_chain_records_excluded_from_subagent_scope(self):
        mixed = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "父链敏感任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:02.000Z",
             "message": {"content": [{"type": "text", "text": "父链回答"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:00:10.000Z", "userType": "external",
             "isSidechain": True, "message": {"content": "子任务输入"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:20.000Z", "isSidechain": True,
             "message": {"content": [{"type": "text", "text": "子任务回答"}]}},
        ])
        stats = ln.parse_transcript(mixed, sidechain=True)
        self.assertEqual(stats["user_prompt"], "子任务输入")
        self.assertEqual(stats["assistant_text"], "子任务回答")
        self.assertNotIn("父链", stats["user_prompt"] + stats["assistant_text"])


class TestStrictConfigValidation(ConfigSandbox):
    """主动自查：JSON 里的 "false"/0/"60" 在 Python 真值语境下行为反直觉，逐项锁死。"""

    def test_illegal_quiet_hours_is_always_quiet(self):
        for bad in ("22-8", [22], [1, 2, 3], {"from": 22}, 22):
            cfg = {"events": ["Stop"], "quiet_hours": bad}
            ln.validate_config(cfg, [])
            self.assertTrue(ln.in_quiet_hours(cfg["quiet_hours"]),
                            "非法 quiet_hours 必须按「一直静默」处理: %r" % (bad,))
            self.assertFalse(ln.should_notify({"hook_event_name": "Stop"}, cfg)[0])

    def test_null_quiet_hours_still_means_no_quiet_period(self):
        cfg = {"events": ["Stop"], "quiet_hours": None}
        ln.validate_config(cfg, [])
        self.assertFalse(ln.in_quiet_hours(cfg["quiet_hours"]))
        self.assertTrue(ln.should_notify({"hook_event_name": "Stop"}, cfg)[0])

    def test_valid_quiet_hours_untouched(self):
        cfg = {"quiet_hours": [22, 8]}
        ln.validate_config(cfg, [])
        self.assertEqual(cfg["quiet_hours"], [22, 8])

    def test_illegal_min_duration_blocks_instead_of_passing(self):
        for bad in ("60", [], True):
            ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                     {"events": ["Stop"], "min_duration_seconds": bad},
                                     {"turn_start": 1000.0, "last_ts": 2000.0})
            self.assertFalse(ok, "非法 min_duration 必须 fail closed: %r" % (bad,))

    def test_epoch_zero_start_still_yields_a_duration(self):
        """时间戳 0 是假值，真值判断会让耗时变成「未知」从而绕过过滤。"""
        self.assertEqual(ln.turn_duration({"turn_start": 0, "last_ts": 30}), 30)
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": 60},
                                 {"turn_start": 0, "last_ts": 30})
        self.assertFalse(ok, "30 秒不该通过 60 秒门槛")

    def test_null_min_duration_means_no_filter(self):
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": None},
                                 {"turn_start": 1000.0, "last_ts": 1001.0})
        self.assertTrue(ok, "null 等同于没设门槛")

    def test_illegal_timeout_falls_back_to_default(self):
        cfg = {"timeout": "abc"}
        w = []
        ln.validate_config(cfg, w)
        self.assertEqual(cfg["timeout"], 5, "传输参数不该因笔误导致再也收不到通知")
        self.assertTrue(w)

    def test_negative_timeout_rejected(self):
        cfg = {"timeout": -3}
        ln.validate_config(cfg, [])
        self.assertEqual(cfg["timeout"], 5)

    def test_illegal_allowed_hosts_falls_back_to_official(self):
        cfg = {"allowed_hosts": "open.feishu.cn"}
        ln.validate_config(cfg, [])
        self.assertEqual(ln.allowed_hosts(cfg), ln.DEFAULT_ALLOWED_HOSTS,
                         "字符串会被逐字符迭代，绝不能当成主机列表")

    def test_illegal_debug_does_not_enable_logging(self):
        cfg = {"debug": "true"}
        ln.validate_config(cfg, [])
        self.assertIs(cfg["debug"], False)

    def test_non_string_credentials_stop_sending(self):
        for key in ("webhook_url", "secret"):
            cfg = {key: ["not", "a", "string"]}
            ln.validate_credentials(cfg, [])
            self.assertIn("_fatal", cfg, key)

    def test_non_string_credentials_stop_sending_end_to_end(self):
        self.write_user({"webhook_url": 12345})
        self.assertIn("_fatal", ln.load_config(str(self.project)))

    def test_valid_config_produces_no_warnings(self):
        cfg = {"webhook_url": SAFE_URL, "secret": "s", "events": ["Stop"],
               "include_summary": True, "min_duration_seconds": 30,
               "quiet_hours": [22, 8], "timeout": 5, "debug": False,
               "allowed_hosts": ["open.feishu.cn"]}
        w = []
        ln.validate_config(cfg, w)
        self.assertEqual(w, [], "合法配置不该产生任何告警")
        self.assertNotIn("_fatal", cfg)

    def test_end_to_end_illegal_quiet_hours_warns_in_doctor(self):
        self.write_user({"webhook_url": SAFE_URL, "quiet_hours": "22-8"})
        cfg = ln.load_config(str(self.project))
        self.assertTrue(any("quiet_hours" in w for w in cfg["_warnings"]), cfg["_warnings"])


# ── codex 第八轮对抗审查回归用例 ────────────────────────────────────

class TestEmptyAllowlistDeniesNotAllows(ConfigSandbox):
    """[medium] 空 allowed_hosts 曾等于关闭主机校验。"""

    def test_empty_list_falls_back_to_official(self):
        cfg = {"allowed_hosts": []}
        w = []
        ln.validate_config(cfg, w)
        self.assertEqual(ln.allowed_hosts(cfg), ln.DEFAULT_ALLOWED_HOSTS)
        self.assertTrue(w)

    def test_attacker_host_rejected_under_empty_list(self):
        ok, why = ln.validate_webhook("https://attacker.example/hook/x",
                                      ln.allowed_hosts({"allowed_hosts": []}))
        self.assertFalse(ok, "空白名单绝不能放行任意主机")

    def test_validate_webhook_with_literally_empty_tuple_denies_all(self):
        ok, _ = ln.validate_webhook("https://open.feishu.cn/x", ())
        self.assertFalse(ok, "空元组意味着一个都不放行")

    def test_non_string_entries_fall_back(self):
        cfg = {"allowed_hosts": [1, 2]}
        ln.validate_config(cfg, [])
        self.assertEqual(ln.allowed_hosts(cfg), ln.DEFAULT_ALLOWED_HOSTS)


class TestValidationIsIdempotent(ConfigSandbox):
    """分层校验会多次调用 validate_config —— 重复校验不得把已收严的值再放宽。"""

    BAD = {"include_summary": "false", "events": "Stop", "quiet_hours": "22-8",
           "min_duration_seconds": "60", "timeout": "abc", "allowed_hosts": "x",
           "debug": "true"}

    def test_repeated_validation_does_not_widen(self):
        cfg = dict(self.BAD)
        ln.validate_config(cfg, [])
        first = dict(cfg)
        for _ in range(3):
            ln.validate_config(cfg, [])
        self.assertEqual(cfg, first, "重复校验必须稳定，不能把收严的值又放宽")
        self.assertFalse(ln.summary_enabled(cfg))
        self.assertTrue(ln.in_quiet_hours(cfg["quiet_hours"]))
        self.assertEqual(ln.effective_events(cfg), [])
        self.assertEqual(ln.allowed_hosts(cfg), ln.DEFAULT_ALLOWED_HOSTS)

    def test_warnings_are_deduplicated(self):
        self.write_user({"webhook_url": SAFE_URL, "quiet_hours": "22-8"})
        self.write_project({"events": ["Stop"]})     # 触发分层校验路径
        cfg = ln.load_config(str(self.project))
        quiet_warnings = [w for w in cfg["_warnings"] if "quiet_hours" in w]
        self.assertEqual(len(quiet_warnings), 1,
                         "同一条告警不该因为多次校验而重复出现: %r" % cfg["_warnings"])


class TestMergeValidatesBeforeCombining(ConfigSandbox):
    """[medium] 先合并后校验，会让仓库配置洗掉用户的非法值限制。"""

    def test_project_cannot_launder_illegal_user_quiet_hours(self):
        self.write_user({"webhook_url": SAFE_URL, "quiet_hours": []})
        self.write_project({"quiet_hours": [22, 8]})
        cfg = ln.load_config(str(self.project))
        self.assertTrue(ln.in_quiet_hours(cfg["quiet_hours"]),
                        "用户的非法值本该触发全天静默，不能被仓库配置洗成合法值")

    def test_project_cannot_widen_timeout_via_illegal_value(self):
        self.write_user({"webhook_url": SAFE_URL, "timeout": 2})
        self.write_project({"timeout": -1})
        cfg = ln.load_config(str(self.project))
        self.assertLessEqual(cfg["timeout"], 2,
                             "非法项目值不得让用户的 2 秒上限放宽")

    def test_project_illegal_min_duration_stays_strict(self):
        self.write_user({"webhook_url": SAFE_URL, "min_duration_seconds": 30})
        self.write_project({"min_duration_seconds": "0"})
        cfg = ln.load_config(str(self.project))
        self.assertGreaterEqual(float(cfg["min_duration_seconds"]), 30)


class TestUnknownDurationFailsClosed(unittest.TestCase):
    """[medium] 耗时算不出来时曾直接放行，门槛在异常情况下自动失效。"""

    def test_missing_timestamps_block_when_threshold_set(self):
        stats = {"user_prompt": "敏感任务", "assistant_text": "回答",
                 "turn_start": None, "last_ts": None}
        ok, reason = ln.should_notify({"hook_event_name": "Stop"},
                                      {"events": ["Stop"], "min_duration_seconds": 60}, stats)
        self.assertFalse(ok)
        self.assertIn("无法确定", reason)

    def test_no_threshold_still_sends_without_timestamps(self):
        stats = {"turn_start": None, "last_ts": None}
        ok, _ = ln.should_notify({"hook_event_name": "Stop"}, {"events": ["Stop"]}, stats)
        self.assertTrue(ok, "没设门槛就不该因为拿不到耗时而不发")

    def test_infinite_threshold_blocks_even_with_unknown_duration(self):
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": "bad"},
                                 {"turn_start": None, "last_ts": None})
        self.assertFalse(ok)


class TestSubagentAttribution(unittest.TestCase):
    """[medium] 没有专属 transcript 时，多个并发 subagent 会被拼成一条对话。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_interleaved_agents_do_not_produce_a_frankenstein_summary(self):
        parent = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "isSidechain": True, "message": {"content": "Agent A 的任务"}},
            {"type": "user", "timestamp": "2026-01-01T00:00:01.000Z", "userType": "external",
             "isSidechain": True, "message": {"content": "Agent B 的任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:09.000Z", "isSidechain": True,
             "message": {"content": [{"type": "text", "text": "Agent A 的回答"}]}},
        ])
        stats = ln.parse_for_event({"hook_event_name": "SubagentStop",
                                    "transcript_path": parent})
        self.assertFalse(stats["summary_reliable"])
        self.assertEqual(stats["user_prompt"], "")
        self.assertEqual(stats["assistant_text"], "")
        card = ln.build_hook_payload(
            {"hook_event_name": "SubagentStop", "cwd": os.getcwd(),
             "transcript_path": parent}, {})
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("Agent B 的任务", blob)
        self.assertNotIn("Agent A 的回答", blob)
        self.assertIn("子任务完成", blob, "元数据卡片仍应发出")

    def test_dedicated_agent_transcript_is_trusted(self):
        agent = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "isSidechain": True, "message": {"content": "唯一子任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:12.000Z", "isSidechain": True,
             "message": {"content": [{"type": "text", "text": "子任务结论"}]}},
        ])
        stats = ln.parse_for_event({"hook_event_name": "SubagentStop",
                                    "transcript_path": "/parent.jsonl",
                                    "agent_transcript_path": agent})
        self.assertTrue(stats["summary_reliable"])
        self.assertEqual(stats["user_prompt"], "唯一子任务")

    def test_stop_event_is_always_reliable(self):
        path = self._write([
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "userType": "external",
             "message": {"content": "主链任务"}},
        ])
        stats = ln.parse_for_event({"hook_event_name": "Stop", "transcript_path": path})
        self.assertTrue(stats["summary_reliable"])
        self.assertEqual(stats["user_prompt"], "主链任务")


# ── codex 第九轮对抗审查回归用例 ────────────────────────────────────

class TestEnvOverrideClearsLowPriorityFatal(ConfigSandbox):
    """[medium] 低优先级凭据出错，不该让最高优先级的环境变量失效。"""

    def test_env_url_wins_over_illegal_user_credential(self):
        self.write_user({"webhook_url": 12345})
        self.write_project({"events": ["Stop"]})     # 触发分层校验路径
        os.environ["LARK_WEBHOOK_URL"] = SAFE_URL
        self.addCleanup(os.environ.pop, "LARK_WEBHOOK_URL", None)
        cfg = ln.load_config(str(self.project))
        self.assertNotIn("_fatal", cfg, "环境变量已提供合法地址，不该被早先的 _fatal 卡住")
        self.assertEqual(cfg["webhook_url"], SAFE_URL)

    def test_behaviour_does_not_depend_on_project_config_presence(self):
        """有没有项目配置，不该改变「非法用户凭据 + 合法环境变量」的结果。"""
        self.write_user({"webhook_url": 12345})
        os.environ["LARK_WEBHOOK_URL"] = SAFE_URL
        self.addCleanup(os.environ.pop, "LARK_WEBHOOK_URL", None)
        without = ln.load_config(str(self.project))
        self.write_project({"events": ["Stop"]})
        with_project = ln.load_config(str(self.project))
        self.assertEqual("_fatal" in without, "_fatal" in with_project)
        self.assertNotIn("_fatal", with_project)

    def test_illegal_credential_without_env_still_stops(self):
        self.write_user({"webhook_url": 12345})
        self.assertIn("_fatal", ln.load_config(str(self.project)))


class TestNonFiniteNumbersRejected(ConfigSandbox):
    """[medium] NaN 让 `dur < 门槛` 恒为 False，门槛看似启用实则永远放行。"""

    def test_is_number_rejects_nan_and_infinity(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertFalse(ln._is_number(bad), repr(bad))

    def test_nan_threshold_does_not_pass_everything(self):
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": float("nan")},
                                 {"turn_start": 0.0, "last_ts": 1.0})
        self.assertFalse(ok, "NaN 门槛必须 fail closed，而不是放行一切")

    def test_negative_infinity_threshold_does_not_pass_everything(self):
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": float("-inf")},
                                 {"turn_start": 0.0, "last_ts": 1.0})
        self.assertFalse(ok)

    def test_config_file_rejects_nan_literal(self):
        path = self.project / ".claude" / "lark.json"
        path.write_text('{"min_duration_seconds": NaN}', encoding="utf-8")
        cfg = ln.load_config(str(self.project))
        self.assertIn("_fatal", cfg, "标准 JSON 不允许 NaN 字面量，应视为坏配置")

    def test_config_file_rejects_infinity_literal(self):
        path = self.project / ".claude" / "lark.json"
        path.write_text('{"timeout": Infinity}', encoding="utf-8")
        self.assertIn("_fatal", ln.load_config(str(self.project)))


class TestTruncatedLongTurnStillNotifies(unittest.TestCase):
    """[medium] 我在上一轮加的 fail-closed 会系统性漏掉最该提醒的长任务。"""

    def test_lower_bound_rescues_truncated_long_turn(self):
        """整轮大于尾部上限时起点被截掉，但尾部跨度已能证明「至少跑了这么久」。"""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        line = json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "text", "text": "x" * 900}]}}) + "\n"
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "userType": "external",
                                "timestamp": "2026-01-01T00:00:00.000Z",
                                "message": {"content": "一个超长的工具密集任务"}},
                               ensure_ascii=False) + "\n")
            for _ in range((ln.MAX_TRANSCRIPT_BYTES // len(line)) + 400):
                f.write(line)
            f.write(json.dumps({"type": "assistant",
                                "timestamp": "2026-01-01T00:05:00.000Z",
                                "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}) + "\n")
            f.write(json.dumps({"type": "assistant",
                                "timestamp": "2026-01-01T02:00:00.000Z",
                                "message": {"content": [{"type": "text", "text": "终于跑完了"}]}},
                               ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        stats = ln.parse_transcript(path)
        self.assertIsNone(stats["turn_start"], "起点应已被截断丢弃")
        self.assertIsNotNone(ln.duration_lower_bound(stats))
        ok, reason = ln.should_notify({"hook_event_name": "Stop", "transcript_path": path},
                                      {"events": ["Stop"], "min_duration_seconds": 60}, stats)
        self.assertTrue(ok, "尾部跨度已远超门槛，不该漏掉这个长任务: %s" % reason)

    def test_lower_bound_does_not_span_a_reset_boundary(self):
        """坏行 reset 之后，下界只能覆盖边界之后的时间，否则短任务会混过门槛。"""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "userType": "external",
                                "timestamp": "2026-01-01T00:00:00.000Z",
                                "message": {"content": "很久以前的任务"}},
                               ensure_ascii=False) + "\n")
            f.write('{"type": "user", "message": {"content": "损坏的边界行\n')
            f.write(json.dumps({"type": "assistant",
                                "timestamp": "2026-01-01T03:00:03.000Z",
                                "message": {"content": [{"type": "text", "text": "3 秒的短活"}]}},
                               ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        stats = ln.parse_transcript(path)
        self.assertIsNone(stats["turn_start"])
        lower = ln.duration_lower_bound(stats)
        self.assertTrue(lower is None or lower < 60,
                        "下界不得横跨 reset 之前的 3 小时，实际 %r" % (lower,))
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": 3600}, stats)
        self.assertFalse(ok, "3 秒的活不能混过 1 小时门槛")

    def test_lower_bound_does_not_rescue_a_genuinely_short_turn(self):
        stats = {"turn_start": None, "earliest_ts": 1000.0, "last_ts": 1003.0}
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": 60}, stats)
        self.assertFalse(ok, "下界只有 3 秒，不能放行")

    def test_no_timestamps_at_all_still_fails_closed(self):
        stats = {"turn_start": None, "earliest_ts": None, "last_ts": None}
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": 60}, stats)
        self.assertFalse(ok)

    def test_known_duration_still_wins(self):
        stats = {"turn_start": 1000.0, "earliest_ts": 0.0, "last_ts": 1005.0}
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": 60}, stats)
        self.assertFalse(ok, "已知耗时 5 秒，不能被下界 1005 秒顶替")


class TestSelectorMustBeString(ConfigSandbox):
    """[medium] 假值选择器被当成「没配」，静默发往默认群。"""

    def test_falsey_non_string_selectors_stop_sending(self):
        for bad in ([], {}, 0, False, "", "   "):
            self.write_user({"webhook_url": SAFE_URL})
            self.write_project({"webhook": bad})
            cfg = ln.load_config(str(self.project))
            self.assertIn("_fatal", cfg, "选择器 %r 必须停发而不是回落默认群" % (bad,))

    def test_valid_selector_still_resolves(self):
        self.write_user({"webhook_url": SAFE_URL,
                         "webhooks": {"群": {"webhook_url": SAFE_URL + "-x"}}})
        self.write_project({"webhook": "群"})
        cfg = ln.load_config(str(self.project))
        self.assertNotIn("_fatal", cfg)
        self.assertEqual(cfg["webhook_url"], SAFE_URL + "-x")


# ── codex 第十轮对抗审查回归用例 ────────────────────────────────────

class TestEarliestTsPerTurn(unittest.TestCase):
    """[medium] 新一轮缺时间戳时，下界不得沿用上一轮的起点。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_new_turn_without_timestamp_does_not_inherit_lower_bound(self):
        path = self._write([
            {"type": "user", "userType": "external",
             "timestamp": "2026-01-01T00:00:00.000Z",
             "message": {"content": "很久以前的任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T02:59:00.000Z",
             "message": {"content": [{"type": "text", "text": "旧回答"}]}},
            # 新一轮的用户记录没有 timestamp
            {"type": "user", "userType": "external", "message": {"content": "新任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T03:00:03.000Z",
             "message": {"content": [{"type": "text", "text": "3 秒搞定"}]}},
        ])
        stats = ln.parse_transcript(path)
        self.assertEqual(stats["user_prompt"], "新任务")
        self.assertIsNone(stats["turn_start"])
        lower = ln.duration_lower_bound(stats)
        self.assertTrue(lower is None or lower < 60,
                        "下界不得横跨上一轮，实际 %r" % (lower,))
        ok, _ = ln.should_notify({"hook_event_name": "Stop"},
                                 {"events": ["Stop"], "min_duration_seconds": 3600}, stats)
        self.assertFalse(ok, "3 秒的新任务不能混过 1 小时门槛")

    def test_malformed_timestamp_behaves_the_same(self):
        path = self._write([
            {"type": "user", "userType": "external",
             "timestamp": "2026-01-01T00:00:00.000Z", "message": {"content": "旧任务"}},
            {"type": "user", "userType": "external",
             "timestamp": "not-a-timestamp", "message": {"content": "新任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T03:00:03.000Z",
             "message": {"content": [{"type": "text", "text": "好了"}]}},
        ])
        stats = ln.parse_transcript(path)
        lower = ln.duration_lower_bound(stats)
        self.assertTrue(lower is None or lower < 60, "实际 %r" % (lower,))

    def test_normal_turn_duration_unaffected(self):
        path = self._write([
            {"type": "user", "userType": "external",
             "timestamp": "2026-01-01T00:00:00.000Z", "message": {"content": "任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:45.000Z",
             "message": {"content": [{"type": "text", "text": "完成"}]}},
        ])
        self.assertAlmostEqual(ln.turn_duration(ln.parse_transcript(path)), 45.0, places=1)


class TestMalformedHookPayloadSendsNothing(unittest.TestCase):
    """[medium] 一段 `{broken` 曾能伪造出一张「任务完成」卡片。"""

    def _run(self, stdin_bytes):
        """在子进程里跑 hook，并统计 post 被调用了几次。"""
        repo = Path(__file__).resolve().parent.parent
        probe = repo / "tests" / "_probe_malformed.py"
        probe.write_text(
            "import sys\n"
            "sys.path.insert(0, %r)\n" % str(repo / "scripts") +
            "import lark_notify as ln\n"
            "n = []\n"
            "ln.post = lambda *a, **k: (n.append(1), (True, 'ok'))[1]\n"
            "ln.load_config = lambda *a, **k: {'webhook_url': 'https://open.feishu.cn/x',\n"
            "                                  '_warnings': []}\n"
            "rc = ln.main(['hook'])\n"
            "print('rc=%s calls=%d' % (rc, len(n)))\n",
            encoding="utf-8")
        self.addCleanup(probe.unlink, True)
        proc = subprocess.run([sys.executable, str(probe)], input=stdin_bytes,
                              capture_output=True, text=True, timeout=30)
        return proc.stdout.strip(), proc.stderr

    def test_broken_json_sends_nothing(self):
        out, err = self._run("{broken")
        self.assertIn("calls=0", out, "畸形输入绝不能发出通知。stderr=%s" % err[:300])
        self.assertIn("rc=0", out)

    def test_empty_stdin_sends_nothing(self):
        out, _ = self._run("")
        self.assertIn("calls=0", out)

    def test_non_object_payload_sends_nothing(self):
        out, _ = self._run("[1, 2, 3]")
        self.assertIn("calls=0", out)

    def test_oversized_payload_sends_nothing(self):
        out, _ = self._run(json.dumps({"hook_event_name": "Stop",
                                       "pad": "x" * (ln.MAX_STDIN_BYTES + 100)}))
        self.assertIn("calls=0", out, "被截断的 payload 内容已不完整，不能拿来发通知")

    def test_valid_payload_still_sends(self):
        out, err = self._run(json.dumps({"hook_event_name": "Stop", "cwd": os.getcwd()}))
        self.assertIn("calls=1", out, "正常 payload 必须照发。stderr=%s" % err[:300])


class TestCliOverrideIsHighestPriority(ConfigSandbox):
    """[medium] 低层凭据非法时，合法的 --webhook 也被挡住了。"""

    def test_cli_clears_credential_fatal(self):
        cfg = {"_fatal": "webhook_url 必须是字符串", "_fatal_kind": "credentials",
               "webhook_url": 123, "secret": "old"}
        out = ln.apply_cli_override(cfg, SAFE_URL)
        self.assertNotIn("_fatal", out)
        self.assertEqual(out["webhook_url"], SAFE_URL)
        self.assertNotIn("secret", out, "换了地址不该继续用旧密钥签名")

    def test_cli_does_not_clear_config_fatal(self):
        cfg = {"_fatal": "项目配置不是合法 JSON"}
        out = ln.apply_cli_override(cfg, SAFE_URL)
        self.assertIn("_fatal", out, "坏配置的 fatal 不该被 --webhook 掩盖")

    def test_no_cli_webhook_is_a_noop(self):
        cfg = {"webhook_url": SAFE_URL, "secret": "s"}
        self.assertEqual(ln.apply_cli_override(cfg, ""), cfg)

    def test_send_with_cli_webhook_over_illegal_config(self):
        self.write_user({"webhook_url": 12345})
        args = ln.build_parser().parse_args(
            ["send", "-t", "x", "-m", "y", "--webhook", SAFE_URL, "--dry-run"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ln.cmd_send(args)
        self.assertEqual(rc, 0, "配置里的非法地址不该挡住合法的 --webhook")
        self.assertIn("interactive", buf.getvalue())


# ── 卡片排版回归用例 ────────────────────────────────────────────────

class TestCardHasNoBlankLines(unittest.TestCase):
    """元素自带前导 \n 又被 "\n".join 加一次，会在每两个元素间插出一个空行。

    注意这些断言只针对**卡片自身的排版骨架**，所以用例里的内容都是单行的。
    用户提问和 Claude 回复本身可能含空行（markdown 段落），那是内容不是排版，
    必须原样保留 —— 见 test_multiline_content_is_preserved。
    """

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def _body(self, card):
        return card["card"]["body"]["elements"][0]["content"]

    def test_hook_card_body_has_no_double_newline(self):
        path = self._write([
            {"type": "user", "userType": "external",
             "timestamp": "2026-01-01T00:00:00.000Z",
             "message": {"content": "把登录接口改成 JWT"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:20.000Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:05:00.000Z",
             "message": {"content": [{"type": "text", "text": "已完成，12 个测试通过。"}]}},
        ])
        body = self._body(ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {}))
        self.assertNotIn("\n\n", body, "元素之间不该出现空行:\n%s" % body)
        # 内容仍然齐全
        for expect in ("把登录接口改成 JWT", "已完成，12 个测试通过。", "本轮任务", "完成情况"):
            self.assertIn(expect, body)

    def test_multiline_content_is_preserved(self):
        """不能为了压缩密度去动用户/助手自己的换行。"""
        answer = "第一段\n\n## 小标题\n\n第二段"
        path = self._write([
            {"type": "user", "userType": "external",
             "timestamp": "2026-01-01T00:00:00.000Z", "message": {"content": "任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:20.000Z",
             "message": {"content": [{"type": "text", "text": answer}]}},
        ])
        body = self._body(ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {}))
        self.assertIn(answer, body, "助手回复里的段落结构应原样保留")

    def test_scaffolding_joins_with_single_newline(self):
        """直接锁住骨架：标题行与各分节之间恰好一个换行。"""
        path = self._write([
            {"type": "user", "userType": "external",
             "timestamp": "2026-01-01T00:00:00.000Z", "message": {"content": "单行任务"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:20.000Z",
             "message": {"content": [{"type": "text", "text": "单行回答"}]}},
        ])
        body = self._body(ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {}))
        self.assertIn("**Claude Code 已完成本轮任务**\n**📋 本轮任务**\n单行任务\n"
                      "**📝 完成情况**\n单行回答\n", body, body)

    def test_notification_card_body_has_no_double_newline(self):
        card = ln.build_hook_payload(
            {"hook_event_name": "Notification", "cwd": os.getcwd(),
             "message": "Claude needs your permission to use Bash"}, {})
        self.assertNotIn("\n\n", self._body(card))

    def test_session_end_card_body_has_no_double_newline(self):
        card = ln.build_hook_payload(
            {"hook_event_name": "SessionEnd", "cwd": os.getcwd(), "reason": "exit"},
            {"events": ["SessionEnd"]})
        self.assertNotIn("\n\n", self._body(card))

    def test_send_card_body_has_no_double_newline(self):
        args = ln.build_parser().parse_args(
            ["send", "-t", "构建失败", "-s", "failed", "-m", "分支 feat/x 编译不通过",
             "-d", "error[E0308]", "--no-session", "--dry-run"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ln.cmd_send(args)
        body = self._body(json.loads(buf.getvalue()))
        self.assertNotIn("\n\n", body, body)
        for expect in ("构建失败", "分支 feat/x 编译不通过", "error[E0308]"):
            self.assertIn(expect, body)


class TestCardTitleUsesSessionName(unittest.TestCase):
    """一个项目下会并行跑很多任务，标题全是项目名就分不清是哪件事。"""

    def _write(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def _title(self, card):
        return card["card"]["header"]["title"]["content"]

    def test_subject_prefers_session_name(self):
        self.assertEqual(ln.card_subject("重构登录模块", "my-project"), "重构登录模块")

    def test_subject_falls_back_to_project(self):
        for empty in ("", "   ", None):
            self.assertEqual(ln.card_subject(empty, "my-project"), "my-project")

    def test_subject_is_truncated(self):
        long_name = "很长的会话名" * 20
        subject = ln.card_subject(long_name, "p")
        self.assertLessEqual(len(subject), ln.MAX_SUBJECT_CHARS)

    def test_hook_title_uses_session_name(self):
        path = self._write([
            {"type": "ai-title", "aiTitle": "重构登录模块", "sessionId": "s1"},
            {"type": "user", "userType": "external",
             "timestamp": "2026-01-01T00:00:00.000Z", "message": {"content": "任务"}},
        ])
        title = self._title(ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(),
             "session_id": "s1", "transcript_path": path}, {}))
        self.assertIn("重构登录模块", title)
        self.assertIn("任务完成", title)

    def test_hook_title_falls_back_to_project_without_session_name(self):
        path = self._write([
            {"type": "user", "userType": "external",
             "timestamp": "2026-01-01T00:00:00.000Z", "message": {"content": "任务"}},
        ])
        title = self._title(ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(), "transcript_path": path}, {}))
        self.assertIn(os.path.basename(os.getcwd()) or "claude-hook-lark", title)

    def test_session_name_not_duplicated_in_body_or_fields(self):
        path = self._write([
            {"type": "ai-title", "aiTitle": "重构登录模块", "sessionId": "s1"},
            {"type": "user", "userType": "external",
             "timestamp": "2026-01-01T00:00:00.000Z", "message": {"content": "任务"}},
        ])
        card = ln.build_hook_payload(
            {"hook_event_name": "Stop", "cwd": os.getcwd(),
             "session_id": "s1", "transcript_path": path}, {})
        body = card["card"]["body"]["elements"][0]["content"]
        self.assertNotIn("重构登录模块", body, "标题已有会话名，正文不该再重复")
        fields_blob = json.dumps(card["card"]["body"]["elements"][2]["fields"], ensure_ascii=False)
        self.assertNotIn("重构登录模块", fields_blob, "字段区同理")
        self.assertIn("s1", fields_blob, "但完整 session ID 仍要保留")

    def test_no_session_flag_keeps_project_name(self):
        args = ln.build_parser().parse_args(
            ["send", "-t", "x", "-m", "y", "--no-session", "--dry-run"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ln.cmd_send(args)
        card = json.loads(buf.getvalue())
        self.assertNotIn("会话 ID", json.dumps(card, ensure_ascii=False))


class TestVersionMetadata(unittest.TestCase):
    """版本号写在两个文件里，改一个忘一个是很容易犯的错。"""

    def _repo(self):
        return Path(__file__).resolve().parent.parent

    def _load(self, rel):
        return json.loads((self._repo() / rel).read_text(encoding="utf-8"))

    def test_plugin_and_marketplace_versions_match(self):
        plugin = self._load(".claude-plugin/plugin.json")["version"]
        market = self._load(".claude-plugin/marketplace.json")["metadata"]["version"]
        self.assertEqual(plugin, market,
                         "plugin.json 与 marketplace.json 的版本号必须一致，"
                         "否则 marketplace 判断更新时会看到矛盾的信息")

    def test_version_is_semver(self):
        version = self._load(".claude-plugin/plugin.json")["version"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$", "版本号应为 x.y.z")

    def test_changelog_documents_current_version(self):
        version = self._load(".claude-plugin/plugin.json")["version"]
        changelog = (self._repo() / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("[%s]" % version, changelog,
                      "发布新版本时 CHANGELOG 要同步记一笔")


if __name__ == "__main__":
    unittest.main(verbosity=2)
