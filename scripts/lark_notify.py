#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude-hook-lark —— 把 Claude Code 的事件推送到飞书 / Lark。

零第三方依赖，只使用 Python 标准库。

三个子命令：
  hook    从 stdin 读取 Claude Code hook 事件 JSON，自动组装卡片并推送
  send    手动发送一条通知（供 skill、脚本或命令行调用）
  doctor  检查配置是否可用，可选发送一条测试消息

配置查找顺序（先命中先用）：
  1. 命令行 --webhook
  2. 环境变量 LARK_WEBHOOK_URL / FEISHU_WEBHOOK_URL
  3. 环境变量 CLAUDE_HOOK_LARK_CONFIG 指向的 JSON 文件
  4. 项目内 <cwd>/.claude/lark.json
  5. 用户级 ~/.config/claude-hook-lark/config.json
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import hmac
import json
import math
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "claude-hook-lark"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "debug.log"

DEFAULT_EVENTS = ["Stop", "Notification"]
MAX_SUMMARY_CHARS = 600

# hook 里的总时间预算。plugin.json 给的外部硬超时是 15 秒，被杀掉的话
# 顶层 except 和 exit 0 都不会执行，用户会看到 hook timeout。留足余量。
HOOK_BUDGET_SECONDS = 9.0
MAX_STDIN_BYTES = 1 << 20          # 1 MiB，hook 事件远小于此
MAX_CONFIG_BYTES = 256 << 10      # 配置文件上限，防止特殊文件无界读取
MAX_RESPONSE_BYTES = 64 << 10     # 只读取有限的响应体
MAX_TRANSCRIPT_BYTES = 16 << 20    # 只读尾部 16 MiB，够覆盖最近若干轮

# 通知目的地只允许飞书/Lark 官方域名，避免会话内容被发到任意主机
DEFAULT_ALLOWED_HOSTS = ("open.feishu.cn", "open.larksuite.com", "open.f.mioffice.cn")

# 仓库里的 .claude/lark.json 只能收窄行为，不能指定「发到哪」
PROJECT_SAFE_KEYS = frozenset({
    "webhook", "events", "include_summary", "min_duration_seconds",
    "quiet_hours", "timeout",
})
DESTINATION_KEYS = frozenset({"webhook_url", "secret", "allowed_hosts", "webhooks"})

# 状态 → (图标, 中文, 飞书卡片颜色)
STATUS_STYLES = {
    "success": ("✅", "成功", "green"),
    "failed": ("❌", "失败", "red"),
    "running": ("⏳", "进行中", "blue"),
    "decision": ("🤔", "待决策", "orange"),
    "warning": ("⚠️", "警告", "yellow"),
    "error": ("⚠️", "异常", "carmine"),
    "info": ("💬", "信息", "wathet"),
}

VALID_COLORS = {
    "blue", "wathet", "turquoise", "green", "yellow", "orange",
    "red", "carmine", "violet", "purple", "indigo", "grey",
}


# ── 基础工具 ────────────────────────────────────────────────────────

def log(msg: str, enabled: bool = True) -> None:
    """写调试日志。永不抛异常——hook 里不能因为日志失败而中断。

    日志目录 0700、文件 0600：webhook 路径本身等同凭据，即使脱敏后也不该让
    同机其他用户随手读到会话摘要。
    """
    if not enabled:
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        existed = LOG_PATH.exists()
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
        if not existed:
            os.chmod(LOG_PATH, 0o600)
    except Exception:
        pass


def truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _read_text_bounded(path: Path, limit: int, allow_symlink: bool) -> tuple[str | None, str]:
    """有界读取一个普通文件。返回 (文本, 错误说明)。

    不可信来源（仓库里的项目配置）必须拒绝符号链接和字符设备：
    提交一个 `.claude/lark.json -> /dev/zero` 就能让每次 hook 无限读取、
    撑爆内存或耗尽外部超时，而这条路径在 json.load 内部，时间预算管不到。
    """
    flags = os.O_RDONLY
    if not allow_symlink:
        # O_NOFOLLOW 只保护最后一段路径，父目录若是符号链接仍可被指到别处
        try:
            if os.path.islink(str(path.parent)):
                return None, "%s 的父目录是符号链接，出于安全考虑拒绝读取" % path
        except OSError:
            pass
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        # FIFO 的 O_RDONLY 会一直阻塞到出现写端；O_NONBLOCK 让它立刻返回，
        # 随后的 S_ISREG 检查再把它挡掉
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return None, ""
    except IsADirectoryError:
        return None, "%s 是目录，已跳过" % path
    except OSError as e:
        if e.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            return None, "%s 是符号链接，出于安全考虑拒绝读取" % path
        return None, "%s 读取失败（errno %s）" % (path, errno.errorcode.get(e.errno, e.errno))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "%s 不是普通文件，拒绝读取" % path
        # 不能只信 st_size：特殊文件会报 0。多读一个字节来判超限。
        raw = os.read(fd, limit + 1)
    except OSError as e:
        return None, "%s 读取失败（errno %s）" % (path, errno.errorcode.get(e.errno, e.errno))
    finally:
        os.close(fd)
    if len(raw) > limit:
        return None, "%s 超过 %d KiB，拒绝读取" % (path, limit >> 10)
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "%s 不是合法的 UTF-8 文本" % path


def read_json_file(path: Path, allow_symlink: bool = True) -> tuple[dict | None, str]:
    """读 JSON 配置。返回 (内容, 错误说明)。

    区分「文件不存在」和「文件存在但坏了」—— 后者必须让用户看见，
    静默降级到别的配置会把通知发到意料之外的群。
    """
    text, err = _read_text_bounded(path, MAX_CONFIG_BYTES, allow_symlink)
    if err:
        return None, err
    if text is None:
        return None, ""
    try:
        data = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as e:
        return None, "%s 不是合法 JSON（第 %d 行）" % (path, e.lineno)
    except ValueError as e:
        return None, "%s 含非法数值：%s" % (path, e)
    if not isinstance(data, dict):
        return None, "%s 顶层必须是一个 JSON 对象" % path
    return data, ""


class HookTimeout(BaseException):
    """总预算耗尽。只在 hook 模式使用，捕获后安静退出 0。

    刻意继承 BaseException 而不是 Exception：本文件里到处是 `except Exception`
    的兜底（读 stdin、写日志、跑 git），继承 Exception 会让看门狗被就地吞掉，
    然后带着空 payload 继续往下走，最终把一张错误的卡片发出去。
    """


def install_watchdog(seconds: float):
    """装一个真实墙钟看门狗。

    Budget 只能约束「我们主动传超时的调用」，管不到 stdin 等 EOF、
    json 解析、文件读取这些内部阻塞。SIGALRM 是唯一能无差别打断它们的手段。
    返回可用于恢复的旧 handler；平台不支持时返回 None。
    """
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return None

    def _fire(signum, frame):
        raise HookTimeout()

    try:
        old = signal.signal(signal.SIGALRM, _fire)
    except ValueError:
        return None      # 不在主线程
    signal.setitimer(signal.ITIMER_REAL, max(0.5, seconds))
    return old


def cancel_watchdog(old) -> None:
    if old is None:
        return
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    except (ValueError, OSError):
        pass


class Budget:
    """单调时钟总预算，保证 hook 整体耗时可控。"""

    def __init__(self, total: float = HOOK_BUDGET_SECONDS):
        self.deadline = time.monotonic() + max(0.5, total)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def slice(self, want: float) -> float:
        """取一段不超过剩余预算的时间片。"""
        return max(0.1, min(want, self.remaining()))


def load_config(cwd: str | None = None) -> dict:
    """按信任级别装配配置。

    信任模型：**通知目的地只能来自用户自己掌握的位置** —— 环境变量、
    ~/.config/claude-hook-lark/config.json、或 CLAUDE_HOOK_LARK_CONFIG 指向的文件。
    仓库里的 <cwd>/.claude/lark.json 会随 git clone 进入机器，因此只允许它
    收窄行为（开关事件、静默时段等），或用 "webhook" 字段**按名字**引用
    用户配置里已登记的地址，绝不能自带 URL。
    """
    cfg: dict = {}
    warnings: list[str] = []
    registry: dict = {}     # 具名 webhook 只从可信来源累积

    # 1) 用户级配置（可信）
    user_cfg, err = read_json_file(CONFIG_PATH)
    if err:
        warnings.append(err)
    if user_cfg:
        cfg.update(user_cfg)
        if isinstance(user_cfg.get("webhooks"), dict):
            registry.update(user_cfg["webhooks"])
        cfg["_source"] = str(CONFIG_PATH)

    # 2) 显式指定的配置文件（可信——是用户自己设的环境变量）
    explicit = os.environ.get("CLAUDE_HOOK_LARK_CONFIG")
    if explicit:
        data, err = read_json_file(Path(explicit))
        if err or data is None:
            # 用户明确指定了配置文件却读不成：停发。回落到用户级配置
            # 等于把内容发到一个他刚刚显式覆盖掉的群。
            msg = err or "CLAUDE_HOOK_LARK_CONFIG 指向的文件不存在：%s" % explicit
            warnings.append(msg)
            cfg["_fatal"] = msg
        else:
            # 显式配置一旦生效就完全接管目的地：先清掉从用户级继承来的
            # webhook_url / secret / 具名表，否则一个 {} 或漏写 webhook_url 的配置
            # 会静默沿用上一级的群，等于绕过了「显式配置失效不得回退」。
            for key in ("webhook_url", "secret", "webhook", "webhooks"):
                cfg.pop(key, None)
            registry.clear()
            cfg.update(data)
            if isinstance(data.get("webhooks"), dict):
                registry.update(data["webhooks"])
            cfg["_source"] = "%s (CLAUDE_HOOK_LARK_CONFIG)" % explicit

    # 3) 项目级配置（不可信：只取安全键）
    project_path = Path(cwd or os.getcwd()) / ".claude" / "lark.json"
    # allow_symlink=False：这是不可信来源，不能跟随符号链接到特殊文件
    project_cfg, err = read_json_file(project_path, allow_symlink=False)
    if err:
        # 项目配置坏了就整体停发，而不是悄悄退回全局群
        warnings.append(err)
        cfg["_fatal"] = err
    elif project_cfg:
        rejected = sorted(set(project_cfg) & DESTINATION_KEYS)
        if rejected:
            warnings.append(
                "已忽略项目配置 %s 中的 %s：仓库内文件不允许指定通知目的地。"
                "如需按项目分流，请在用户配置的 webhooks 里登记后用 \"webhook\": \"名字\" 引用。"
                % (project_path, "、".join(rejected))
            )
        safe = {k: v for k, v in project_cfg.items() if k in PROJECT_SAFE_KEYS}
        if safe:
            # 必须**先各自校验再合并**：直接拿原始值合并的话，
            #   用户 quiet_hours:[] 非法 + 项目 [22,8] => 合并出合法值，
            #   用户本该触发的「全天静默」被仓库配置洗掉了；
            #   用户 timeout:2 + 项目 timeout:-1 => 先并成 -1，再被校验器改回 5，
            #   仓库反而把用户的 2 秒上限放宽成 5 秒。
            validate_config(cfg, warnings)
            validate_config(safe, warnings)
            cfg = merge_project_config(cfg, safe, warnings)
            cfg["_project_source"] = str(project_path)

    # 4) 具名 webhook：把名字解析成可信来源里登记的地址+密钥（成对加载）
    validate_selector(cfg, warnings)
    name = cfg.pop("webhook", "")
    if name:
        entry = registry.get(name)
        if isinstance(entry, str):
            entry = {"webhook_url": entry}
        if isinstance(entry, dict) and entry.get("webhook_url"):
            cfg["webhook_url"] = entry["webhook_url"]
            cfg["secret"] = entry.get("secret", "")   # 成对覆盖，避免 URL/密钥错配
            cfg["_source"] = "%s → webhooks[%s]" % (cfg.get("_source", "用户配置"), name)
        else:
            msg = "配置引用了未登记的 webhook 名称 \"%s\"，已停止发送。" % name
            warnings.append(msg)
            cfg["_fatal"] = msg

    # 5) 环境变量优先级最高（可信）
    env_url = os.environ.get("LARK_WEBHOOK_URL") or os.environ.get("FEISHU_WEBHOOK_URL")
    if env_url:
        cfg["webhook_url"] = env_url
        cfg["secret"] = os.environ.get("LARK_WEBHOOK_SECRET") or os.environ.get(
            "FEISHU_WEBHOOK_SECRET") or ""
        cfg["_source"] = "环境变量 LARK_WEBHOOK_URL/FEISHU_WEBHOOK_URL"
    else:
        env_secret = os.environ.get("LARK_WEBHOOK_SECRET") or os.environ.get("FEISHU_WEBHOOK_SECRET")
        if env_secret:
            cfg["secret"] = env_secret

    validate_config(cfg, warnings)
    # 凭据放到最后校验：此时环境变量已经覆盖过，不会因为低优先级的错误值
    # 留下一个撤销不掉的 _fatal
    validate_credentials(cfg, warnings)

    # 分层校验会让同一条告警重复出现（值是幂等的，告警不是）。去重但保持顺序。
    seen = set()
    cfg["_warnings"] = [w for w in warnings if not (w in seen or seen.add(w))]
    return cfg


def _type_name(value) -> str:
    return type(value).__name__


def validate_credentials(cfg: dict, warnings: list) -> None:
    """校验凭据类字段。**只在所有层级合并完之后调用一次**。

    早校验会留下无法撤销的 _fatal：用户级 webhook_url 类型写错、但环境变量
    （最高优先级）给了正确地址时，通知本该正常发出，却因为早先写下的 _fatal
    被跳过 —— 而且这个行为还取决于「项目配置存不存在」，因为那才会触发早校验。
    """
    for key in ("webhook_url", "secret"):
        if key in cfg and not isinstance(cfg[key], str):
            msg = "%s 必须是字符串（当前是 %s），已停止发送。" % (key, _type_name(cfg[key]))
            warnings.append(msg)
            cfg["_fatal"] = msg
            # 标明这条 fatal 只是因为凭据类型不对 —— 更高优先级的来源
            # （环境变量、命令行 --webhook）给出合法地址时可以撤销它
            cfg["_fatal_kind"] = "credentials"


def validate_selector(cfg: dict, warnings: list) -> None:
    """具名选择器必须是非空字符串。

    `webhook: []` / `{}` / `0` / `false` 都是假值，会被当成「没配」而静默沿用
    默认群 —— 这是把内容发错地方，必须和「引用了未登记的名字」一样停发。
    """
    if "webhook" not in cfg:
        return
    value = cfg["webhook"]
    if not isinstance(value, str) or not value.strip():
        msg = ("webhook 必须是已登记的名称字符串（当前是 %s），已停止发送。"
               % _type_name(value))
        warnings.append(msg)
        cfg["_fatal"] = msg
        cfg.pop("webhook", None)


def validate_config(cfg: dict, warnings: list) -> None:
    """逐项严格校验配置类型。

    这里统一处理，而不是散落在各个使用点做真值判断 —— JSON 里的 "false"、0、
    "60" 在 Python 真值语境下的行为全都出人意料，散着写必然漏。

    原则：
      隐私与过滤开关类型非法 → 取**最严**解释（宁可不发，也不能悄悄多发）
      传输参数类型非法       → 退回安全默认值（不该因为一个笔误就再也收不到通知）
      凭据类型非法           → 直接停发
    """
    # —— 隐私开关：非 boolean 一律按 false ——
    if "include_summary" in cfg and not isinstance(cfg["include_summary"], bool):
        warnings.append(
            "include_summary 必须是 true 或 false（当前是 %s），已按 false 处理，卡片不含会话摘要。"
            % _type_name(cfg["include_summary"]))
        cfg["include_summary"] = False

    if "debug" in cfg and not isinstance(cfg["debug"], bool):
        warnings.append("debug 必须是 true 或 false（当前是 %s），已按 false 处理。"
                        % _type_name(cfg["debug"]))
        cfg["debug"] = False

    # —— 事件过滤：非数组按「一个都不发」 ——
    if "events" in cfg and not isinstance(cfg["events"], list):
        warnings.append("events 必须是数组（当前是 %s），已按「不推送任何事件」处理。"
                        % _type_name(cfg["events"]))

    # —— 静默时段：null 是合法的「不静默」；其余非法值按「一直静默」 ——
    if cfg.get("quiet_hours") is not None and "quiet_hours" in cfg:
        if not _is_hour_pair(cfg["quiet_hours"]):
            warnings.append(
                "quiet_hours 必须是形如 [22, 8] 的两个整数（当前是 %s），"
                "已按「一直静默」处理，期间不会推送。" % _type_name(cfg["quiet_hours"]))
            cfg["quiet_hours"] = "__invalid__"

    # —— 最短耗时：非数字按「一直不满足」，也就是不发 ——
    if ("min_duration_seconds" in cfg and cfg["min_duration_seconds"] is not None
            and not _is_number(cfg["min_duration_seconds"])):
        warnings.append(
            "min_duration_seconds 必须是数字（当前是 %s），已按「不推送」处理。"
            % _type_name(cfg["min_duration_seconds"]))
        cfg["min_duration_seconds"] = float("inf")

    # —— 超时：传输参数，非法就退回默认值，不该因此收不到通知 ——
    if "timeout" in cfg and (not _is_number(cfg["timeout"]) or float(cfg["timeout"]) < 0):
        warnings.append("timeout 必须是非负数字（当前是 %r），已按默认 5 秒处理。"
                        % (cfg["timeout"],))
        cfg["timeout"] = 5

    # —— 主机白名单：非数组就退回官方域名，绝不放宽 ——
    if "allowed_hosts" in cfg:
        hosts = cfg["allowed_hosts"]
        if not isinstance(hosts, (list, tuple)):
            warnings.append("allowed_hosts 必须是数组（当前是 %s），已退回官方域名白名单。"
                            % _type_name(hosts))
            cfg.pop("allowed_hosts")
        elif not hosts:
            # 空数组曾被当成「不限制」，等于把目的地校验整个关掉
            warnings.append("allowed_hosts 为空数组，已退回官方域名白名单（空列表不代表放行所有主机）。")
            cfg.pop("allowed_hosts")
        elif not all(isinstance(h, str) and h.strip() for h in hosts):
            warnings.append("allowed_hosts 必须是非空字符串数组，已退回官方域名白名单。")
            cfg.pop("allowed_hosts")


def _is_number(value) -> bool:
    """有限实数才算数字。

    NaN 尤其危险：`dur < nan` 恒为 False，min_duration_seconds 设成 NaN 会让
    门槛看似启用、实则永远放行。±Infinity 同理。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _reject_json_constant(name):
    # Python 的 json 默认接受 NaN / Infinity / -Infinity 字面量，标准 JSON 并不允许
    raise ValueError("配置中不允许 %s" % name)


def _is_hour_pair(value) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    return all(_is_number(v) and 0 <= int(v) <= 24 for v in value)


def merge_project_config(cfg: dict, project: dict, warnings: list | None = None) -> dict:
    """把项目配置单调地合并进可信配置：**只能更严，不能更松**。

    直接 update 的话，仓库就能把用户关掉的 include_summary 重新打开、
    把 quiet_hours 抹掉、把 events 扩大 —— 与「项目只能收窄」的信任模型相反。
    """
    out = dict(cfg)

    # events 取交集：只能减少，不能新增事件
    if "events" in project:
        if isinstance(project["events"], list):
            out["events"] = [e for e in effective_events(cfg) if e in project["events"]]
        else:
            # 光忽略是不够的：上层若也没配 events，最终这个键就不存在，
            # effective_events 会回落到默认值，非法配置反而放宽了权限。
            out["events"] = []
            if warnings is not None:
                warnings.append(
                    "项目配置的 events 必须是数组（当前是 %s），已按「不推送任何事件」处理。"
                    % type(project["events"]).__name__)

    # include_summary 只能由 true 收窄为 false
    if "include_summary" in project and project["include_summary"] is not True:
        # 只能由 true 收窄为 false；非法类型也一并按 false 处理（fail closed）
        out["include_summary"] = False

    # 最短时长取较大值（过滤更严）
    if "min_duration_seconds" in project:
        try:
            out["min_duration_seconds"] = max(
                float(cfg.get("min_duration_seconds") or 0),
                float(project["min_duration_seconds"]),
            )
        except (TypeError, ValueError):
            pass

    # 超时取较小值（更快放弃）
    if "timeout" in project:
        try:
            # 不能写 `cfg.get("timeout") or 5`：显式的 0 是假值，会被当成没配，
            # 于是 min(0, 10) 变成了 5，项目配置反而把超时放宽了。
            current = float(cfg["timeout"]) if "timeout" in cfg else 5.0
            out["timeout"] = min(current, float(project["timeout"]))
        except (TypeError, ValueError):
            pass

    # 静默时段：用户没设时项目可以加；用户设过就不允许被改写或取消
    if project.get("quiet_hours") and not cfg.get("quiet_hours"):
        out["quiet_hours"] = project["quiet_hours"]

    # 具名 webhook 引用原样带过（含非法值），稍后由 validate_selector 统一裁决 ——
    # 这里用真值判断会让 [] / 0 / false 被静默丢弃，从而退回默认群。
    if "webhook" in project:
        out["webhook"] = project["webhook"]

    return out


def apply_cli_override(cfg: dict, webhook: str) -> dict:
    """把命令行 --webhook 作为**最高优先级**并入配置。

    以前 CLI 覆盖发生在 _fatal 检查之后，于是「配置里的 webhook_url 类型写错」
    会让一个完全合法的 --webhook 也发不出去 —— 与文档声明的优先级相悖。
    """
    if not webhook:
        return cfg
    cfg = dict(cfg)
    cfg["webhook_url"] = webhook
    cfg.pop("secret", None)          # 换了地址就不该继续用旧密钥签名
    if cfg.get("_fatal_kind") == "credentials":
        cfg.pop("_fatal", None)
        cfg.pop("_fatal_kind", None)
    return cfg


def allowed_hosts(cfg: dict) -> tuple:
    hosts = cfg.get("allowed_hosts", None)
    if isinstance(hosts, (list, tuple)) and hosts:
        return tuple(str(h).lower() for h in hosts)
    return DEFAULT_ALLOWED_HOSTS


def validate_webhook(url: str, hosts: tuple = DEFAULT_ALLOWED_HOSTS) -> tuple[bool, str]:
    """校验目的地。返回 (是否通过, 原因)。

    原因里**只允许出现主机名**，绝不能回显完整 URL —— URL 里的 token 等同密码，
    一旦进日志就等于泄漏。
    """
    if not url:
        return False, "未配置 webhook_url"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False, "webhook URL 无法解析"
    if parts.scheme != "https":
        return False, "webhook 必须使用 https（当前为 %s）" % (parts.scheme or "空")
    try:
        host = (parts.hostname or "").lower()
    except ValueError:
        return False, "webhook URL 的主机名非法"
    if not host:
        return False, "webhook URL 缺少主机名"
    # 无条件校验成员关系：空白名单意味着「一个都不放行」，而不是「全部放行」
    if host not in hosts:
        return False, "主机 %s 不在允许列表内（可在用户配置的 allowed_hosts 中放行）" % host
    return True, ""


def mask(url: str) -> str:
    """脱敏展示。飞书 webhook 的 token 在路径末段，只保留固定前缀。"""
    if not url:
        return "(未配置)"
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or "?"
    except ValueError:
        return "(无法解析的 URL)"
    return "%s://%s/…/%s" % (parts.scheme or "?", host, "*" * 8)


# ── 发送 ────────────────────────────────────────────────────────────

# 飞书业务错误码 → 我们自己的静态说明。
# 只收录亲自验证过含义的码；其余一律只报数字，不猜、也不回显远端文案。
KNOWN_ERROR_CODES = {
    19021: "签名校验失败，或时间戳超出 1 小时（检查 secret 是否与机器人一致）",
}


def _normalize_code(value) -> int | None:
    """把远端 code 规范成有限范围内的整数；不合规一律丢弃，不回显原值。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if -1_000_000 < value < 1_000_000 else None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            num = int(value.strip())
        except ValueError:
            return None
        return num if -1_000_000 < num < 1_000_000 else None
    return None


def describe_error_code(code) -> str:
    try:
        return KNOWN_ERROR_CODES[int(code)]
    except (TypeError, ValueError, KeyError):
        return "含义参见飞书开放平台文档"


def scrub_url(text: str, url: str) -> str:
    """从任意文本里抹掉 webhook URL 及其 token 片段。

    远端返回的字符串不可信 —— 一旦它回显了请求路径，落盘就等于泄漏凭据。
    """
    if not text or not url:
        return text
    secrets = {url}
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.path:
            secrets.add(parts.path)
            tail = parts.path.rstrip("/").rsplit("/", 1)[-1]
            if len(tail) >= 8:
                secrets.add(tail)
        if parts.query:
            secrets.add(parts.query)
    except ValueError:
        pass
    expanded = set()
    for piece in secrets:
        if not piece or len(piece) < 8:
            continue
        expanded.add(piece)
        # 代理常把 URL 百分号编码后回显
        expanded.add(urllib.parse.quote(piece, safe=""))
        expanded.add(urllib.parse.quote(piece, safe="/"))
    for piece in sorted(expanded, key=len, reverse=True):
        # 大小写无关地替换：主机名和编码后的十六进制大小写都可能变化
        idx = text.lower().find(piece.lower())
        while idx != -1:
            text = text[:idx] + "***" + text[idx + len(piece):]
            idx = text.lower().find(piece.lower(), idx + 3)
    return text


def sign(secret: str, timestamp: str) -> str:
    """飞书自定义机器人签名校验：以 '<timestamp>\\n<secret>' 为 key，空串为消息体。"""
    string_to_sign = "%s\n%s" % (timestamp, secret)
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def post(webhook_url: str, payload: dict, secret: str = "", timeout: float = 5.0,
         hosts: tuple = DEFAULT_ALLOWED_HOSTS) -> tuple[bool, str]:
    """POST 到飞书 webhook。返回 (是否成功, 说明)。不抛异常，说明里不含 URL。"""
    ok, why = validate_webhook(webhook_url, hosts)
    if not ok:
        return False, why
    body = dict(payload)
    if secret:
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = sign(secret, ts)

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        # Request() 对畸形 URL 抛的 ValueError 消息里含完整 URL，必须就地拦下
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
    except ValueError:
        return False, "webhook URL 格式非法（已隐去内容）"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            raw = resp.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # 绝不回显响应正文：代理或错误页可能把请求 URL（含 token）原样打回来
            return False, "响应不是 JSON（HTTP %s，%d 字节）" % (status, len(raw))
        # 必须先规范化再判成功：Python 里 False == 0、0.0 == 0，
        # 直接写 `raw in (0, None)` 会把 {"code": false}、{"code": null}
        # 乃至缺失 code 的畸形响应当成发送成功。
        raw_code = parsed.get("code", parsed.get("StatusCode"))
        code = _normalize_code(raw_code)
        if code is None:
            return False, "响应错误码格式非法或缺失（正文已省略）"
        if code == 0:
            return True, "ok"
        # 绝不回显远端文案。脱敏是模式匹配，挡不住任意编码变体
        #（例如把连字符也百分号编码），所以这里直接换成我们自己的静态说明。
        return False, "飞书返回 code=%s（%s）" % (code, describe_error_code(code))
    except urllib.error.HTTPError as e:
        # 同理，HTTPError 的 body 不可信，只保留状态码
        return False, "HTTP %s（响应正文已省略）" % e.code
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        # 不要格式化异常本身：URLError 的 reason 里可能带上完整请求 URL
        inner = getattr(e, "reason", None)
        return False, "网络错误: %s" % type(inner if inner is not None else e).__name__


# ── 卡片构建 ────────────────────────────────────────────────────────

def field(label: str, value: str, short: bool = True) -> dict:
    return {
        "is_short": short,
        "text": {"tag": "lark_md", "content": "**%s**\n%s" % (label, value or "-")},
    }


def build_card(title: str, color: str, body: str, fields: list[dict], footer: str) -> dict:
    """构建飞书卡片（schema 2.0）。

    为什么用 2.0 而不是 v1：v1 的正文是 `div` + `lark_md`，而 lark_md 只支持
    粗体、行内代码、链接这一小撮语法 —— Claude 回复里常见的标题（##）、列表、
    代码块、引用统统会以原文形式露出来。2.0 的 `markdown` 元素支持完整得多。

    2.0 的几个坑（都是对着真实 API 试出来的）：
      - `note` 元素**不被接受**（code 11246），无论 elements 里放 plain_text、
        markdown 还是 text，也无论换成 `text` 字段。脚注只能用 markdown 元素代替。
      - `hr`、`div`（含 fields 写法）、`column_set` 在 2.0 里仍然可用。
      - 元素挂在 `body.elements` 下，不再是顶层 `elements`。
    """
    elements: list[dict] = [{"tag": "markdown", "content": body or "-"}]
    if fields:
        elements.append({"tag": "hr"})
        # fields 是 div 独有的能力，2.0 里依然有效；它只需要粗体，lark_md 够用
        elements.append({"tag": "div", "fields": fields})
    elements.append({"tag": "markdown",
                     "content": "<font color='grey'>%s</font>" % footer})
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color if color in VALID_COLORS else "blue",
            },
            "body": {"elements": elements},
        },
    }


# ── 运行环境信息 ────────────────────────────────────────────────────

def git_info(cwd: str, budget: "Budget | None" = None) -> dict:
    info = {"repo": "", "branch": "", "uncommitted": 0}

    def run(args: list[str]) -> str:
        limit = budget.slice(1.5) if budget else 3
        try:
            out = subprocess.run(
                ["git", "-C", cwd] + args,
                capture_output=True, text=True, timeout=limit,
            )
            return out.stdout.strip() if out.returncode == 0 else ""
        except Exception:
            return ""

    top = run(["rev-parse", "--show-toplevel"])
    if not top:
        return info
    info["repo"] = os.path.basename(top)
    info["branch"] = run(["branch", "--show-current"]) or "(detached)"
    status = run(["status", "--porcelain", "--untracked-files=all"])
    info["uncommitted"] = len([l for l in status.splitlines() if l.strip()])
    return info


def project_name(cwd: str, git: dict) -> str:
    return git.get("repo") or os.path.basename(os.path.normpath(cwd)) or cwd


def hostname() -> str:
    try:
        return socket.gethostname().split(".")[0]
    except Exception:
        return "unknown"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分 %d 秒" % (seconds // 60, seconds % 60)
    return "%d 小时 %d 分" % (seconds // 3600, (seconds % 3600) // 60)


# ── transcript 解析 ─────────────────────────────────────────────────

def parse_ts(value) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def reset_turn_state(stats: dict) -> None:
    """断开一轮：任务、回答、工具计数、起始时间、轮次性质一起清掉。

    earliest_ts 也必须清掉，否则耗时下界会横跨边界之前的时间：
    「文件很早开始 → 中间损坏 → 之后一个 3 秒的短轮次」会算出好几小时的下界，
    反而让短任务通过 min_duration_seconds。清空后由边界之后的第一条记录重新播种。
    """
    stats["user_prompt"] = ""
    stats["assistant_text"] = ""
    stats["tool_calls"] = 0
    stats["turn_start"] = None
    stats["turn_kind"] = ""
    stats["earliest_ts"] = None


def parse_for_event(payload: dict) -> dict:
    """按事件作用域解析 transcript，并标注摘要是否可信。

    SubagentStop 缺少 agent_transcript_path 时，只能退回父文件，但父文件里
    **多个并发 subagent 的 sidechain 记录是交错的**，没有可靠的身份字段能把它们
    分开 —— 「A 提问、B 提问、A 回答」会被拼成「B 的任务 + A 的回答」，
    既是错误归因，也可能把另一个子任务的内容带出去。
    这种情况下只取元数据（会话名、分支），不出摘要。
    """
    event = payload.get("hook_event_name") or "Stop"
    if event == "SubagentStop":
        agent_path = (payload.get("agent_transcript_path")
                      or payload.get("agentTranscriptPath") or "")
        if agent_path:
            stats = parse_transcript(agent_path, sidechain=True)
            stats["summary_reliable"] = True
            return stats
        stats = parse_transcript(payload.get("transcript_path", ""), sidechain=False)
        reset_turn_state(stats)
        stats["summary_reliable"] = False
        return stats
    stats = parse_transcript(payload.get("transcript_path", ""), sidechain=False)
    stats["summary_reliable"] = True
    return stats


def transcript_for(payload: dict) -> tuple[str, bool]:
    """选出该事件应该解析哪份 transcript，以及是否按 subagent 作用域解析。

    SubagentStop 的 payload 会带 agent_transcript_path，里面的记录都是
    isSidechain=true。主链解析会跳过它们，所以子任务卡片必须换个作用域读，
    否则拿不到子任务的输入、回答、工具数和耗时，反而可能显示父链的旧摘要。
    """
    if (payload.get("hook_event_name") or "") == "SubagentStop":
        agent_path = (payload.get("agent_transcript_path")
                      or payload.get("agentTranscriptPath") or "")
        if agent_path:
            return agent_path, True
        # 没给专属文件时退回主文件，但仍按 subagent 作用域读
        return payload.get("transcript_path", ""), True
    return payload.get("transcript_path", ""), False


def parse_transcript(path: str, sidechain: bool = False) -> dict:
    """从会话 transcript(JSONL) 中提取本轮的用户提问、最后一条助手回复、工具调用数等。

    文件不存在 / 格式异常一律降级为空结果，绝不抛异常。
    """
    stats = {
        "user_prompt": "",
        "assistant_text": "",
        "tool_calls": 0,
        "turn_start": None,
        "last_ts": None,
        "model": "",
        "git_branch": "",
        "session_name": "",
        "session_id": "",
        "turn_kind": "",
        "earliest_ts": None,   # 已解析范围内最早的时间戳，用于给耗时定下界
    }
    if not path:
        return stats
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            try:
                size = os.fstat(f.fileno()).st_size
                if size > MAX_TRANSCRIPT_BYTES:
                    # 超大 transcript 只看尾部；丢掉被切断的首行
                    f.seek(size - MAX_TRANSCRIPT_BYTES)
                    f.readline()
            except (OSError, ValueError):
                pass
            pending_corrupt = False
            for raw_line in f:
                # strip() 之前先记住这行有没有以换行结尾 —— 这是区分
                # 「已完整落盘的坏记录」和「正在追加、写了一半的尾行」的**唯一**依据。
                # 先 strip 再判断的话，两者长得一模一样。
                terminated = raw_line.endswith("\n")
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    if terminated or pending_corrupt:
                        # 以换行结尾却解析失败 => 这条记录已经写完了，就是坏的，
                        # 不是「还没写完」。它有可能正是一条轮次边界，fail closed。
                        # 连续两条坏行同理：追加写被打断只会留下一条半截尾行。
                        reset_turn_state(stats)
                    pending_corrupt = True
                    continue
                if not isinstance(obj, dict):
                    if terminated or pending_corrupt:
                        reset_turn_state(stats)
                    pending_corrupt = True
                    continue

                if pending_corrupt:
                    # 坏行后面还有正常记录 => 是**文件中间**损坏，那条坏行有可能
                    # 正是一条轮次边界。无法确认就 fail closed，清掉已累计的内容，
                    # 免得把上一轮的任务和回答带到本轮卡片上。
                    #
                    # 单独一条位于文件末尾的坏行则照常容忍：Stop 触发时 Claude Code
                    # 往往正在追加写，尾行经常是半截的。要是连它也 fail closed，
                    # 卡片就会长期空白 —— 那是把常态当异常处理。
                    pending_corrupt = False
                    reset_turn_state(stats)

                # sidechain 是 subagent 的对话，不属于主链这一轮。
                # 这个判断必须放在 last_ts / assistant_text / tool_calls 之前 ——
                # 否则一条晚到的 subagent 记录会覆盖父轮回答、把它的工具计入父轮，
                # 还会拉长耗时，让短任务绕过 min_duration_seconds。
                # 作用域必须在更新 last_ts / assistant_text / tool_calls **之前**判定：
                # 否则一条越界记录会覆盖本轮回答、把它的工具计入本轮，还会拉长耗时，
                # 让短任务绕过 min_duration_seconds。
                if bool(obj.get("isSidechain")) != sidechain:
                    continue

                if obj.get("gitBranch"):
                    stats["git_branch"] = obj["gitBranch"]

                # 会话名：Claude Code 会写入 ai-title / agent-name 记录，
                # 用户改名后会追加新记录，因此取文件中最后出现的那条。
                for key in ("aiTitle", "agentName", "customName", "title"):
                    if obj.get(key):
                        stats["session_name"] = str(obj[key])
                        break
                sid = obj.get("sessionId") or obj.get("session_id")
                if sid:
                    stats["session_id"] = str(sid)

                ts = parse_ts(obj.get("timestamp"))
                if ts:
                    stats["last_ts"] = ts
                    if stats["earliest_ts"] is None:
                        stats["earliest_ts"] = ts

                rtype = obj.get("type")
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}

                if rtype == "user":
                    # 只有真人输入才算新一轮。type=user 里混着三类非真人记录：
                    #   toolUseResult  —— 工具返回结果
                    #   isMeta         —— skill/slash command 展开出来的提示词
                    #   isSidechain    —— subagent 的对话
                    # 漏掉任何一类，卡片的「本轮任务」就会显示成系统文本。
                    is_real_user = (
                        obj.get("userType", "external") == "external"
                        and "toolUseResult" not in obj
                        and not obj.get("isMeta")
                        and not obj.get("isCompactSummary")
                        and not obj.get("isVisibleInTranscriptOnly")
                        and not has_tool_result(msg.get("content"))
                    )
                    if is_real_user:
                        # 每条真人记录都是一道**完整**边界：任务、回答、工具计数、
                        # 起始时间一起断开。
                        #
                        # 只清任务是不够的 —— 上一轮的回答里往往复述了任务内容，
                        # 留着它等于换个地方泄漏；不重置 turn_start / tool_calls
                        # 还会让耗时横跨两轮，并让短任务绕过 min_duration_seconds。
                        #
                        # text 为空的两种情况同样要重置：纯图片/附件消息，
                        # 以及以保留标签开头的内容（斜杠命令、系统注入 —— 后者没有
                        # 可信字段能证明来自系统，用户完全可以自己粘一段
                        # <task-notification> 让 Claude 分析）。
                        kind, text = classify_user_text(extract_text(msg.get("content")))
                        # 走 reset_turn_state 而不是逐字段赋值：earliest_ts 也必须
                        # 一并清掉。否则新一轮的用户记录若缺时间戳或时间戳畸形，
                        # turn_start 会是 None，而 earliest_ts 还停在上一轮，
                        # 算出的「下界」横跨两轮，让未知耗时的新任务混过门槛。
                        reset_turn_state(stats)
                        stats["turn_kind"] = kind
                        stats["user_prompt"] = text
                        stats["turn_start"] = ts
                        stats["earliest_ts"] = ts

                elif rtype == "assistant" and msg:
                    if msg.get("model"):
                        stats["model"] = msg["model"]
                    content = msg.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use":
                                stats["tool_calls"] += 1
                    text = extract_text(content)
                    if text:
                        stats["assistant_text"] = text
    except (FileNotFoundError, OSError, UnicodeError):
        pass
    return stats


def find_transcript(session_id: str) -> str:
    """按 session id 在 ~/.claude/projects/*/ 下定位 transcript 文件。

    手动 send 时没有 hook payload，只能靠 CLAUDE_CODE_SESSION_ID 反查。
    用 glob 而不是自己拼「cwd 路径转义成目录名」，避免依赖内部编码规则。
    """
    if not session_id:
        return ""
    try:
        matches = sorted(
            (Path.home() / ".claude" / "projects").glob("*/%s.jsonl" % session_id),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return str(matches[0]) if matches else ""
    except (OSError, ValueError):
        return ""


def session_context(payload: dict | None = None, stats: dict | None = None) -> tuple[str, str]:
    """返回 (session_id, session_name)，任一项取不到就是空串。"""
    payload = payload or {}
    session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        or (stats or {}).get("session_id", "")
    )
    session_name = (stats or {}).get("session_name", "")
    if not session_name and session_id:
        # send 模式：没有现成 stats，按 session id 找回 transcript 读会话名
        path = payload.get("transcript_path") or find_transcript(session_id)
        if path:
            session_name = parse_transcript(path).get("session_name", "")
    return session_id, session_name


MAX_SUBJECT_CHARS = 40


def card_subject(session_name: str, project: str) -> str:
    """卡片标题里跟在事件名后面的主语。

    优先用**会话名**而不是项目名：一个项目下会并行跑很多个任务，
    标题全是项目名的话，一眼看不出通知的是哪件事。会话名按任务归属，
    天然更有区分度。会话刚开始、标题还没生成时退回项目名。
    """
    name = (session_name or "").strip()
    return truncate(name, MAX_SUBJECT_CHARS) if name else project


def session_fields(session_id: str) -> list[dict]:
    """会话 ID 与项目/分支/主机同排展示，不单独占一行。

    会话名不再单列字段 —— 它已经在标题里了，重复一遍纯属浪费卡片空间。
    """
    if not session_id:
        return []
    return [field("会话 ID", "`%s`" % session_id)]


# Claude Code 会把 slash command、本地命令输出、记忆输入等也写成 type=user 的记录，
# 而且不带 isMeta 标记。这些不是用户交给 Claude 的任务，不能进卡片。
# 系统注入，根本不是用户动作，不构成轮次边界。
# 判定规则：只有用户**亲手输入**的内容才算一轮任务。这些记录同样是
# type=user / userType=external，其中一部分（如 task-notification）连 isMeta
# 都没有，光靠结构化字段区分不出来，只能按标签识别。
# 这份清单可能随 Claude Code 版本增加；漏掉一个的后果是卡片上任务那行显示成
# 系统文本，属于观感问题，不影响安全边界。
OUTPUT_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<task-notification>",
    "<user-prompt-submit-hook>",
    "<post-tool-use-hook>",
)
# 用户敲的斜杠命令。有些是纯控制（/model），有些会真正驱动一轮工作（/review、
# 自定义命令）—— 光看这条记录无法可靠区分。两种猜错的代价不对称：
# 猜「非边界」会把上一轮任务原文再发一次（泄漏），猜「边界」只是少显示一行任务。
# 所以一律当作边界，且不带任务文本。
COMMAND_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<bash-input>",
    "<user-memory-input>",
)
NOISE_PREFIXES = OUTPUT_PREFIXES + COMMAND_PREFIXES

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def has_tool_result(content) -> bool:
    """工具结果不一定有顶层 toolUseResult，也可能只是 content 里的 tool_result block。"""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )


def clean_user_text(text: str) -> str:
    """剥掉附加在真实用户消息上的 system-reminder，并滤掉纯系统噪音记录。

    返回空串表示「这条不算用户任务」。
    """
    text = _SYSTEM_REMINDER_RE.sub("", text or "").strip()
    if not text or text.startswith(NOISE_PREFIXES):
        return ""
    return text


def classify_user_text(text: str) -> tuple[str, str]:
    """判断一条用户记录的性质，返回 (类别, 可展示文本)。

    类别：
      tagged  —— 以保留标签开头（系统注入或斜杠命令）。构成完整轮次边界，
                 且不展示标签文本本身。
      prompt  —— 真正交给 Claude 的一轮任务；文本可能为空（纯图片/附件消息）
    """
    text = _SYSTEM_REMINDER_RE.sub("", text or "").strip()
    if text.startswith(NOISE_PREFIXES):
        return "tagged", ""
    return "prompt", text


def extract_text(content) -> str:
    """从 message.content 里抽出纯文本，兼容字符串与 block 数组两种形态。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


# ── 静默时段 ────────────────────────────────────────────────────────

def in_quiet_hours(quiet, now_hour: int | None = None) -> bool:
    """quiet 形如 [22, 8]，表示 22:00–08:00 静默。跨零点自动处理。

    配置校验会把非法值换成 "__invalid__" 哨兵 —— 那种情况按「一直静默」处理：
    用户明确要求了静默时段，我们没法照办，宁可不发也不能在他以为安静的时候推送。
    """
    if quiet == "__invalid__":
        return True
    if not quiet or not isinstance(quiet, (list, tuple)) or len(quiet) != 2:
        return False
    try:
        start, end = int(quiet[0]), int(quiet[1])
    except (TypeError, ValueError):
        return False
    hour = datetime.now().hour if now_hour is None else now_hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# ── hook 模式 ───────────────────────────────────────────────────────

def event_style(event_name: str, payload: dict) -> tuple[str, str, str]:
    """返回 (标题前缀, 颜色, 正文标题)。"""
    if event_name == "Stop":
        return "✅ 任务完成", "green", "**Claude Code 已完成本轮任务**"
    if event_name == "SubagentStop":
        return "📦 子任务完成", "turquoise", "**子任务执行完成**"
    if event_name == "Notification":
        message = (payload.get("message") or "").strip()
        if "permission" in message.lower() or "授权" in message:
            return "🔐 需要授权", "orange", "**Claude Code 需要你的授权才能继续**"
        return "⏳ 等待输入", "yellow", "**Claude Code 正在等待你的输入**"
    if event_name == "SessionEnd":
        return "👋 会话结束", "grey", "**Claude Code 会话已结束**"
    if event_name == "SessionStart":
        return "🚀 会话开始", "blue", "**Claude Code 会话已启动**"
    return "🔔 Claude Code", "blue", "**状态更新**"


def summary_enabled(cfg: dict) -> bool:
    """摘要开关。键缺失才默认开启；只认真正的 boolean，其余一律按关闭处理。"""
    if "include_summary" not in cfg:
        return True
    return cfg["include_summary"] is True


def build_hook_payload(payload: dict, cfg: dict, stats: dict | None = None,
                       budget: "Budget | None" = None) -> dict:
    event_name = payload.get("hook_event_name") or "Stop"
    cwd = payload.get("cwd") or os.getcwd()
    git = git_info(cwd, budget)
    prefix, color, headline = event_style(event_name, payload)
    name = project_name(cwd, git)

    lines = [headline]

    if event_name == "Notification" and payload.get("message"):
        lines.append("📩 %s" % truncate(payload["message"], 300))

    if event_name == "SessionEnd" and payload.get("reason"):
        lines.append("🚪 结束原因：`%s`" % payload["reason"])

    # 无论是否输出摘要都要读一次 transcript —— 会话名只存在于其中
    if stats is None:
        stats = parse_for_event(payload)
    session_id, session_name = session_context(payload, stats)

    if (summary_enabled(cfg) and stats.get("summary_reliable", True)
            and event_name in ("Stop", "SubagentStop")):
        if stats.get("user_prompt"):
            lines.append("**📋 本轮任务**\n%s" % truncate(stats["user_prompt"], 300))
        elif stats.get("turn_kind") == "tagged":
            # 有边界但没有可展示文本：给个中性说明，好过让这一行凭空消失
            lines.append("**📋 本轮任务**\n_（由命令或系统事件触发，内容不展示）_")
        elif stats.get("turn_kind") == "prompt":
            lines.append("**📋 本轮任务**\n_（本轮输入为图片或附件）_")
        if stats.get("assistant_text"):
            lines.append("**📝 完成情况**\n%s" % truncate(stats["assistant_text"], MAX_SUMMARY_CHARS))

    meta = []
    if stats.get("tool_calls"):
        meta.append("🔧 %d 次工具调用" % stats["tool_calls"])
    duration = turn_duration(stats)
    if duration is not None:
        meta.append("⏱️ 用时 %s" % human_duration(duration))
    if git.get("uncommitted"):
        meta.append("📝 %d 个待提交" % git["uncommitted"])
    if meta:
        lines.append(" · ".join(meta))

    fields = [
        field("项目", name),
        field("分支", git.get("branch") or stats.get("git_branch") or "-"),
        field("主机", hostname()),
    ]
    fields.extend(session_fields(session_id))

    footer = "Claude Code · %s · %s" % (
        event_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return build_card("%s · %s" % (prefix, card_subject(session_name, name)),
                      color, "\n".join(lines), fields, footer)


def duration_lower_bound(stats: dict) -> float | None:
    """在起点丢失时，给本轮耗时一个**可证明的下界**。

    transcript 超过尾部上限时只解析尾部，一个工具密集的长任务可能整轮都比上限还大，
    起始那条用户记录落在截断点之前，turn_start 因此为 None。
    此时「耗时未知就不发」会系统性漏掉最该提醒的长任务 —— 而尾部本身的时间跨度
    已经足以证明「至少跑了这么久」。
    """
    first, last = stats.get("earliest_ts"), stats.get("last_ts")
    if first is not None and last is not None and last >= first:
        return last - first
    return None


def turn_duration(stats: dict) -> float | None:
    start, end = stats.get("turn_start"), stats.get("last_ts")
    # 用 is not None，不要用真值判断：时间戳 0（epoch）是假值，
    # 会让耗时变成「未知」，从而绕过 min_duration_seconds 的过滤。
    if start is not None and end is not None and end >= start:
        return end - start
    return None


def effective_events(cfg: dict) -> list:
    """解析生效的事件列表。

    - 键缺失          → 用默认值
    - 显式空列表      → 拒绝全部（「收窄」不能反过来变成放宽）
    - 键存在但类型非法 → 同样拒绝全部，fail closed
    """
    if "events" not in cfg:
        return list(DEFAULT_EVENTS)
    value = cfg["events"]
    return list(value) if isinstance(value, list) else []


def should_notify(payload: dict, cfg: dict, stats: dict | None = None) -> tuple[bool, str]:
    event_name = payload.get("hook_event_name") or "Stop"
    events = effective_events(cfg)
    if event_name not in events:
        return False, "事件 %s 不在 events=%s 中" % (event_name, events)
    if in_quiet_hours(cfg.get("quiet_hours")):
        return False, "处于静默时段 %s" % (cfg.get("quiet_hours"),)
    # 不能写 `cfg.get(...) or 0`：[]、""、0 全是假值，会被一律当成「没设门槛」，
    # 于是一个类型写错的配置反而把过滤彻底关掉了。
    min_dur = 0
    if "min_duration_seconds" in cfg:
        raw = cfg["min_duration_seconds"]
        if raw is None:
            min_dur = 0                 # null 明确表示不过滤
        elif _is_number(raw):
            min_dur = float(raw)
        else:
            min_dur = float("inf")      # 类型非法 → fail closed
    if min_dur and event_name in ("Stop", "SubagentStop"):
        if stats is None:
            stats = parse_for_event(payload)
        dur = turn_duration(stats)
        if dur is None:
            # 起点丢失（截断/损坏）时先看可证明的下界：尾部跨度已经够长，
            # 就没必要因为「算不准」而漏掉一个真正的长任务。
            lower = duration_lower_bound(stats)
            if lower is not None and lower >= min_dur:
                dur = lower
            else:
                # 下界也不足以证明 → fail closed。放行等于让门槛在异常情况下自动失效。
                return False, ("无法确定本轮耗时，min_duration_seconds=%s 已生效，跳过推送"
                               % min_dur)
        if dur < min_dur:
            return False, "本轮耗时 %.0fs < min_duration_seconds=%s" % (dur, min_dur)
    return True, ""


def cmd_hook(args) -> int:
    budget = Budget()
    watchdog = install_watchdog(HOOK_BUDGET_SECONDS)
    if watchdog is None and hasattr(signal, "SIGALRM"):
        # 装不上就只剩 Budget 这层软保护，值得记一笔便于排查 hook timeout
        log("看门狗未能安装，本次仅有时间预算保护", True)
    try:
        return _run_hook(args, budget)
    except HookTimeout:
        # 预算耗尽：安静退出，绝不让 Claude Code 等到硬超时
        log("hook 超出 %.0f 秒预算，已放弃本次通知" % HOOK_BUDGET_SECONDS, True)
        return 0
    finally:
        cancel_watchdog(watchdog)


def _run_hook(args, budget: Budget) -> int:
    raw = ""
    truncated = False
    try:
        # 多读一个字符用来判断是否被截断 —— 截断的 payload 解析出来可能仍然合法，
        # 但内容已经不完整，不能拿它去发通知
        raw = sys.stdin.read(MAX_STDIN_BYTES + 1)
        truncated = len(raw) > MAX_STDIN_BYTES
    except Exception:
        pass

    # 空输入 / 解析失败 / 非对象 / 超限 一律**不发**。
    # 以前这些都被替换成 {}，再默认成 Stop 事件，于是一段 `{broken` 就能伪造出
    # 一张「任务完成」卡片；反复触发还会变成通知风暴。
    if truncated:
        log("hook payload 超过 %d 字节被截断，跳过推送" % MAX_STDIN_BYTES, True)
        return 0
    if not raw.strip():
        log("hook payload 为空，跳过推送", True)
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("hook payload 不是合法 JSON，跳过推送", True)
        return 0
    if not isinstance(payload, dict):
        log("hook payload 顶层不是对象，跳过推送", True)
        return 0

    cfg = apply_cli_override(load_config(payload.get("cwd")), args.webhook)
    debug = bool(cfg.get("debug"))
    log("hook event=%s raw=%s" % (payload.get("hook_event_name"), raw[:500]), debug)

    # 配置告警一律记录：静默降级会把内容发到意料之外的地方
    for warning in cfg.get("_warnings", []):
        log("配置告警: %s" % warning, True)
    if cfg.get("_fatal"):
        log("配置不可用，已停止发送: %s" % cfg["_fatal"], True)
        if args.dry_run:
            print("skip: %s" % cfg["_fatal"])
        return 0

    # 只解析一次 transcript，供过滤和卡片共用
    stats = parse_for_event(payload)

    ok, reason = should_notify(payload, cfg, stats)
    if not ok:
        log("skip: %s" % reason, debug)
        if args.dry_run:
            print("skip: %s" % reason)
        return 0

    webhook = args.webhook or cfg.get("webhook_url", "")
    card = build_hook_payload(payload, cfg, stats, budget)

    if args.dry_run:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    if not webhook:
        log("未配置 webhook_url，跳过发送", True)
        return 0

    # 网络超时取「配置值」与「剩余预算」的较小者，确保总耗时守在硬超时之内
    timeout = budget.slice(float(cfg.get("timeout", 5)))
    sent, detail = post(webhook, card, cfg.get("secret", ""), timeout, allowed_hosts(cfg))
    log("send %s: %s" % ("ok" if sent else "FAIL", detail), debug or not sent)
    return 0


# ── send 模式 ───────────────────────────────────────────────────────

def cmd_send(args) -> int:
    cfg = apply_cli_override(load_config(), args.webhook)
    status = (args.status or "info").lower()
    icon, status_cn, default_color = STATUS_STYLES.get(status, STATUS_STYLES["info"])
    color = args.color or default_color

    cwd = os.getcwd()
    git = git_info(cwd)
    name = project_name(cwd, git)

    title = args.title or "Claude Code 通知"
    lines = ["**%s %s**" % (icon, title), "状态：%s%s" % (icon, status_cn)]
    if args.message:
        lines.append(truncate(args.message, 1500))
    if args.detail:
        lines.append("---\n%s" % truncate(args.detail, 1500))

    session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    session_name = args.session_name
    if not session_name and not args.no_session:
        _, session_name = session_context({"session_id": session_id})
    fields = [field("项目", name), field("分支", git.get("branch") or "-"), field("主机", hostname())]
    if not args.no_session:
        fields.extend(session_fields(session_id))
    footer = "Claude Code · %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = name if args.no_session else card_subject(session_name, name)
    card = build_card("%s %s · %s" % (icon, title, subject), color, "\n".join(lines), fields, footer)

    if args.dry_run:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    for warning in cfg.get("_warnings", []):
        print("提示：%s" % warning, file=sys.stderr)
    if cfg.get("_fatal"):
        print("错误：%s" % cfg["_fatal"], file=sys.stderr)
        return 2

    webhook = args.webhook or cfg.get("webhook_url", "")
    if not webhook:
        print("错误：未找到 webhook_url。请先运行 doctor 查看配置来源，或设置 LARK_WEBHOOK_URL。", file=sys.stderr)
        return 2

    ok, detail = post(webhook, card, cfg.get("secret", ""), float(cfg.get("timeout", 5)),
                      allowed_hosts(cfg))
    if ok:
        print("已发送到飞书：%s" % title)
        return 0
    print("发送失败：%s" % detail, file=sys.stderr)
    return 1


# ── doctor 模式 ─────────────────────────────────────────────────────

def cmd_doctor(args) -> int:
    cfg = apply_cli_override(load_config(), args.webhook)
    webhook = args.webhook or cfg.get("webhook_url", "")
    print("claude-hook-lark 配置检查")
    print("  Python           : %s" % sys.version.split()[0])
    print("  配置来源         : %s" % cfg.get("_source", "(未找到配置文件)"))
    print("  用户级配置路径   : %s%s" % (CONFIG_PATH, "" if CONFIG_PATH.exists() else "  (不存在)"))
    if cfg.get("_project_source"):
        print("  项目级配置       : %s  (仅允许收窄行为，不能指定目的地)" % cfg["_project_source"])
    print("  webhook_url      : %s" % mask(webhook))
    valid, why = validate_webhook(webhook, allowed_hosts(cfg))
    print("  目的地校验       : %s" % ("通过" if valid else "✗ " + why))
    # allowed_hosts 永远非空（空列表会退回官方白名单），不存在「不限制」这一档
    print("  允许的主机       : %s" % ", ".join(allowed_hosts(cfg)))
    print("  签名密钥         : %s" % ("已配置" if cfg.get("secret") else "未配置（机器人未开启签名校验时正常）"))
    events_now = effective_events(cfg)
    print("  监听事件         : %s%s" % (
        events_now, "  (为空：不会推送任何事件)" if not events_now else ""))
    print("  静默时段         : %s%s" % (cfg.get("quiet_hours") or "无",
                                          "  (当前处于静默中)" if in_quiet_hours(cfg.get("quiet_hours")) else ""))
    print("  最小时长过滤     : %s 秒" % (cfg.get("min_duration_seconds") or 0))
    print("  调试日志         : %s" % LOG_PATH)

    for warning in cfg.get("_warnings", []):
        print("\n⚠️  %s" % warning)
    if cfg.get("_fatal"):
        print("\n✗ 配置不可用，通知已停发：%s" % cfg["_fatal"])
        return 2

    if not valid and webhook:
        print("\n✗ %s" % why)
        return 2

    if not webhook:
        print("\n✗ 未配置 webhook_url。参考 README 的「配置」一节。")
        return 2

    if not args.test:
        print("\n✓ 配置看起来正常。加 --test 可发送一条测试消息。")
        return 0

    card = build_card(
        "🔔 claude-hook-lark 测试",
        "blue",
        "**配置测试成功**\n\n如果你在飞书里看到这条消息，说明 webhook 配置正确。",
        [field("主机", hostname()), field("目录", os.path.basename(os.getcwd()))],
        "Claude Code · %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    ok, detail = post(webhook, card, cfg.get("secret", ""), float(cfg.get("timeout", 5)),
                      allowed_hosts(cfg))
    print("\n%s %s" % ("✓ 测试消息已发送。" if ok else "✗ 发送失败：", "" if ok else detail))
    return 0 if ok else 1


# ── 入口 ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lark_notify", description="Claude Code → 飞书/Lark 通知")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook", help="从 stdin 读取 Claude Code hook 事件并推送")
    h.add_argument("--webhook", default="", help="覆盖配置中的 webhook URL")
    h.add_argument("--dry-run", action="store_true", help="只打印卡片 JSON，不实际发送")
    h.set_defaults(func=cmd_hook)

    s = sub.add_parser("send", help="手动发送一条通知")
    s.add_argument("-t", "--title", default="", help="标题")
    s.add_argument("-s", "--status", default="info",
                   choices=sorted(STATUS_STYLES), help="状态，决定图标与默认颜色")
    s.add_argument("-m", "--message", default="", help="正文")
    s.add_argument("-d", "--detail", default="", help="补充详情，显示在分隔线下方")
    s.add_argument("-c", "--color", default="", help="强制指定卡片颜色，如 green/red/orange")
    s.add_argument("--session-id", default="", dest="session_id",
                   help="会话 ID，默认取环境变量 CLAUDE_CODE_SESSION_ID")
    s.add_argument("--session-name", default="", dest="session_name",
                   help="会话名，默认从 transcript 的 ai-title 记录读取")
    s.add_argument("--no-session", action="store_true",
                   help="不在卡片里附带会话名和会话 ID")
    s.add_argument("--webhook", default="", help="覆盖配置中的 webhook URL")
    s.add_argument("--dry-run", action="store_true", help="只打印卡片 JSON，不实际发送")
    s.set_defaults(func=cmd_send)

    d = sub.add_parser("doctor", help="检查配置")
    d.add_argument("--webhook", default="", help="覆盖配置中的 webhook URL")
    d.add_argument("--test", action="store_true", help="发送一条测试消息")
    d.set_defaults(func=cmd_doctor)
    return p


def _fault_site() -> str:
    """返回出错的文件:行号，便于排查，且不含任何异常内容。"""
    try:
        tb = sys.exc_info()[2]
        while tb.tb_next:
            tb = tb.tb_next
        return "%s:%d" % (os.path.basename(tb.tb_frame.f_code.co_filename), tb.tb_lineno)
    except Exception:
        return "未知"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except HookTimeout:
        sys.exit(0)
    except Exception as exc:  # hook 绝不能让 Claude Code 卡住或报错
        # 只记异常类型：异常文本可能包含完整 webhook URL，而 URL 等同凭据
        log("未捕获异常: %s (位于 %s)" % (type(exc).__name__, _fault_site()), True)
        sys.exit(0 if "hook" in sys.argv else 1)
