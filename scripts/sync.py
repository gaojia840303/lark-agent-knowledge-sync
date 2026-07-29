#!/usr/bin/env python3
"""Incremental Feishu knowledge synchronization for business Agents.

The module deliberately keeps external content in data fields. It never evaluates
source text as instructions and only writes approved proposals to fixed document
tokens from the Agent configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence


APPROVAL_STATES = {"待审核", "已批准", "已拒绝", "需修改", "已应用"}
PLACEHOLDERS = {"", "待配置", "TODO", "todo", "null", "None"}
TARGET_SCOPES = {"public", "private"}
KNOWLEDGE_MODES = {"stable", "dynamic"}
ADMISSION_TYPES = {"proactive_submission", "dedicated_view", "bounded_scan", "explicit_approval", "test_fixture"}
AUTHORITY_LEVELS = {
    "formal_policy",
    "approved_decision",
    "business_owner",
    "validated_experience",
    "reference",
}
DEFAULT_BUDGETS = {
    "max_metadata_items": 100,
    "max_fulltext_items": 20,
    "max_fulltext_chars": 60000,
    "max_candidates": 10,
    "max_proposals": 5,
    "max_runtime_seconds": 600,
    "max_source_api_calls": 100,
}
SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|app[_-]?secret|"
    r"authorization|cookie|client[_-]?secret|private[_-]?key)"
)
SECRET_VALUE_RES = [
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----[\s\S]*?-----END(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|app[_-]?secret)\s*[:=]\s*[^\s,;]+"),
]
INJECTION_MARKERS = [
    "ignore previous",
    "ignore all previous",
    "忽略之前",
    "忽略以上",
    "system prompt",
    "系统提示词",
    "执行命令",
    "删除知识",
    "泄露密钥",
    "bypass",
]


class SyncError(RuntimeError):
    pass


class ConfigError(SyncError):
    pass


class CommandError(SyncError):
    pass


class BudgetExceeded(SyncError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return None
    if value in {"[]", "{}"}:
        return json.loads(value)
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        if value.startswith('"'):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            # Accept the common YAML shorthand used in hand-written Agent
            # configs, for example: [品牌周会, 产品复盘].
            inner = value[1:-1].strip()
            if not inner:
                return []
            return [_parse_scalar(item.strip()) for item in inner.split(",")]
    if value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"内联 YAML 对象必须使用 JSON 语法: {value}") from exc
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def parse_simple_yaml(text: str) -> Any:
    """Parse the conservative YAML subset used by agent-sync.yaml.

    It supports indentation-based mappings/lists, scalar values, and JSON-style
    inline lists/maps. This avoids a runtime PyYAML dependency.
    """

    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(text)

    lines: list[tuple[int, str, int]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ConfigError(f"第 {number} 行不能使用 Tab 缩进")
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip(), number))

    if not lines:
        return {}

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return {}, index
        is_list = lines[index][1].startswith("-")
        container: Any = [] if is_list else {}

        while index < len(lines):
            current_indent, content, number = lines[index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ConfigError(f"第 {number} 行出现意外缩进")

            if is_list:
                if not content.startswith("-"):
                    break
                rest = content[1:].strip()
                if not rest:
                    if index + 1 >= len(lines) or lines[index + 1][0] <= indent:
                        container.append(None)
                        index += 1
                    else:
                        child, index = parse_block(index + 1, lines[index + 1][0])
                        container.append(child)
                    continue
                if ":" in rest and not rest.startswith(("http://", "https://")):
                    key, raw_value = rest.split(":", 1)
                    item: dict[str, Any] = {}
                    key = key.strip()
                    raw_value = raw_value.strip()
                    item[key] = _parse_scalar(raw_value) if raw_value else None
                    index += 1
                    if index < len(lines) and lines[index][0] > indent:
                        child, index = parse_block(index, lines[index][0])
                        if raw_value == "" and len(item) == 1 and not isinstance(child, dict):
                            item[key] = child
                        elif isinstance(child, dict):
                            item.update(child)
                        else:
                            raise ConfigError(f"第 {number} 行的列表对象结构无效")
                    container.append(item)
                else:
                    container.append(_parse_scalar(rest))
                    index += 1
            else:
                if content.startswith("-"):
                    break
                if ":" not in content:
                    raise ConfigError(f"第 {number} 行缺少冒号")
                key, raw_value = content.split(":", 1)
                key, raw_value = key.strip(), raw_value.strip()
                if not key:
                    raise ConfigError(f"第 {number} 行字段名为空")
                index += 1
                if raw_value:
                    container[key] = _parse_scalar(raw_value)
                elif index < len(lines) and lines[index][0] > indent:
                    child, index = parse_block(index, lines[index][0])
                    container[key] = child
                else:
                    container[key] = {}
        return container, index

    result, end = parse_block(0, lines[0][0])
    if end != len(lines):
        _, _, number = lines[end]
        raise ConfigError(f"无法解析第 {number} 行")
    return result


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在: {config_path}")
    parsed = parse_simple_yaml(config_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ConfigError("配置根节点必须是对象")
    validate_config(parsed)
    return parsed, config_path


def _nonempty_list(value: Any) -> bool:
    return isinstance(value, list) and any(str(item).strip() not in PLACEHOLDERS for item in value)


def _active_values(source: Mapping[str, Any], keys: Sequence[str]) -> bool:
    return any(_nonempty_list(source.get(key)) for key in keys)


def _validate_source_policy(source: Mapping[str, Any], label: str, errors: list[str]) -> None:
    admission = str(source.get("admission_type", "")).strip()
    authority = str(source.get("authority_level", "")).strip()
    if admission not in ADMISSION_TYPES - {"test_fixture"}:
        errors.append(
            f"{label}.admission_type 必须为 proactive_submission、dedicated_view、"
            "bounded_scan 或 explicit_approval"
        )
    if str(source.get("source_owner", "")).strip() in PLACEHOLDERS:
        errors.append(f"{label}.source_owner 必填")
    if str(source.get("business_purpose", "")).strip() in PLACEHOLDERS:
        errors.append(f"{label}.business_purpose 必填")
    if authority not in AUTHORITY_LEVELS:
        errors.append(
            f"{label}.authority_level 必须为 formal_policy、approved_decision、"
            "business_owner、validated_experience 或 reference"
        )


def _validate_source_shape(source: Mapping[str, Any], label: str, errors: list[str]) -> None:
    scope = str(source.get("target_scope", "private"))
    mode = str(source.get("knowledge_mode", "stable"))
    if scope not in TARGET_SCOPES:
        errors.append(f"{label}.target_scope 只能是 public 或 private")
    if mode not in KNOWLEDGE_MODES:
        errors.append(f"{label}.knowledge_mode 只能是 stable 或 dynamic")


def validate_config(config: Mapping[str, Any], strict: bool = False) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []
    agent = config.get("agent")
    if not isinstance(agent, Mapping):
        errors.append("缺少 agent 配置")
    else:
        agent_id = str(agent.get("id", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", agent_id):
            errors.append("agent.id 必须为 2-63 位小写字母、数字或短横线")
        for key in ("name", "owner", "role"):
            if str(agent.get(key, "")).strip() in PLACEHOLDERS:
                errors.append(f"agent.{key} 必填")
        for key in ("responsibilities", "decision_scope", "output_contract", "success_metrics"):
            if not _nonempty_list(agent.get(key)):
                errors.append(f"agent.{key} 必须至少配置一项")

    knowledge = config.get("knowledge")
    if not isinstance(knowledge, Mapping):
        errors.append("缺少 knowledge 配置")
    else:
        for key in (
            "wiki_space_id",
            "public_folder_token",
            "private_folder_token",
            "public_document_token",
            "private_document_token",
        ):
            if str(knowledge.get(key, "")).strip() in PLACEHOLDERS:
                message = f"knowledge.{key} 尚未配置"
                (errors if strict else warnings).append(message)
        for left, right, label in (
            ("public_folder_token", "private_folder_token", "公共与专属知识目录"),
            ("public_document_token", "private_document_token", "公共与专属目标文档"),
        ):
            left_value = str(knowledge.get(left, "")).strip()
            right_value = str(knowledge.get(right, "")).strip()
            if left_value not in PLACEHOLDERS and left_value == right_value:
                errors.append(f"{label}不能使用同一个 Token")


    sources = config.get("sources")
    if not isinstance(sources, Mapping):
        errors.append("缺少 sources 配置")
        sources = {}
    relevance_rules = config.get("relevance", {})
    if not isinstance(relevance_rules, Mapping):
        errors.append("relevance 必须是对象")
        relevance_rules = {}
    has_relevance = any(
        _nonempty_list(relevance_rules.get(key))
        for key in ("include_keywords", "related_people", "related_projects")
    )
    max_candidate_chars = relevance_rules.get("max_candidate_chars", 12000)
    if not isinstance(max_candidate_chars, int) or not 1 <= max_candidate_chars <= 100000:
        errors.append("relevance.max_candidate_chars 必须为 1-100000 的整数")

    meetings = sources.get("meetings", {}) if isinstance(sources, Mapping) else {}
    if meetings and not isinstance(meetings, Mapping):
        errors.append("sources.meetings 必须是对象")
    elif isinstance(meetings, Mapping):
        meeting_search = _active_values(meetings, ("keywords", "minutes_keywords"))
        meeting_active = meeting_search or _active_values(meetings, ("minute_tokens",))
        if meeting_active:
            _validate_source_shape(meetings, "sources.meetings", errors)
            _validate_source_policy(meetings, "sources.meetings", errors)
        if _nonempty_list(meetings.get("keywords")) and not _nonempty_list(meetings.get("participant_ids")):
            errors.append("sources.meetings.keywords 搜索必须同时限定 participant_ids")
        if _nonempty_list(meetings.get("minutes_keywords")) and not (
            _nonempty_list(meetings.get("participant_ids")) or _nonempty_list(meetings.get("owner_ids"))
        ):
            errors.append("sources.meetings.minutes_keywords 搜索必须同时限定 participant_ids 或 owner_ids")
        if meeting_search and not has_relevance:
            errors.append("搜索型会议或妙记来源必须配置至少一项业务相关性规则")

    documents = sources.get("documents", {}) if isinstance(sources, Mapping) else {}
    if documents and not isinstance(documents, Mapping):
        errors.append("sources.documents 必须是对象")
    elif isinstance(documents, Mapping):
        document_search = _nonempty_list(documents.get("search_keywords"))
        document_active = document_search or _nonempty_list(documents.get("document_tokens"))
        if document_active:
            _validate_source_shape(documents, "sources.documents", errors)
            _validate_source_policy(documents, "sources.documents", errors)
        if document_search and not _nonempty_list(documents.get("folder_tokens")):
            errors.append("文档关键词搜索必须同时配置非空 sources.documents.folder_tokens")
        if document_search and not has_relevance:
            errors.append("搜索型文档来源必须配置至少一项业务相关性规则")
        if isinstance(knowledge, Mapping):
            target_docs = {
                str(knowledge.get("public_document_token", "")).strip(),
                str(knowledge.get("private_document_token", "")).strip(),
            } - PLACEHOLDERS
            target_folders = {
                str(knowledge.get("public_folder_token", "")).strip(),
                str(knowledge.get("private_folder_token", "")).strip(),
            } - PLACEHOLDERS
            source_docs = {str(item).strip() for item in documents.get("document_tokens", []) or []}
            source_folders = {str(item).strip() for item in documents.get("folder_tokens", []) or []}
            if target_docs & source_docs:
                errors.append("目标知识文档不能同时出现在 sources.documents.document_tokens")
            if target_folders & source_folders:
                errors.append("目标知识目录不能同时出现在 sources.documents.folder_tokens")

    bases = sources.get("bases", []) if isinstance(sources, Mapping) else []
    if bases and not isinstance(bases, list):
        errors.append("sources.bases 必须是列表")
    elif isinstance(bases, list):
        for index, source in enumerate(bases):
            label = f"sources.bases[{index}]"
            if not isinstance(source, Mapping):
                errors.append(f"{label} 必须是对象")
                continue
            _validate_source_shape(source, label, errors)
            if str(source.get("knowledge_mode", "dynamic")) == "stable":
                _validate_source_policy(source, label, errors)
                if str(source.get("view_id", "")).strip() in PLACEHOLDERS:
                    errors.append(f"{label} 稳定知识来源必须配置专用 view_id")

    meegle_sources = sources.get("meegle", []) if isinstance(sources, Mapping) else []
    if meegle_sources and not isinstance(meegle_sources, list):
        errors.append("sources.meegle 必须是列表")
    elif isinstance(meegle_sources, list):
        for index, source in enumerate(meegle_sources):
            label = f"sources.meegle[{index}]"
            if not isinstance(source, Mapping):
                errors.append(f"{label} 必须是对象")
                continue
            _validate_source_shape(source, label, errors)
            if str(source.get("knowledge_mode", "dynamic")) == "stable":
                _validate_source_policy(source, label, errors)
                if not (_nonempty_list(source.get("view_ids")) or _nonempty_list(source.get("mql"))):
                    errors.append(f"{label} 稳定知识来源必须配置专用 view_ids 或 mql")

    review = config.get("review", {})
    if not isinstance(review, Mapping):
        errors.append("review 必须是对象")
    elif review.get("storage", "local") not in {"local", "base"}:
        errors.append("review.storage 只能是 local 或 base")
    elif review.get("storage") == "base":
        if str(review.get("base_token", "")).strip() in PLACEHOLDERS:
            errors.append("Base 存储必须配置 review.base_token")
        tables = review.get("tables", {})
        for key in ("proposals", "runs", "cursors"):
            if not isinstance(tables, Mapping) or str(tables.get(key, "")).strip() in PLACEHOLDERS:
                errors.append(f"Base 存储必须配置 review.tables.{key}")

    fixtures = sources.get("fixtures", []) if isinstance(sources, Mapping) else []
    if fixtures and (not isinstance(review, Mapping) or review.get("storage", "local") != "local"):
        errors.append("sources.fixtures 只允许与 review.storage: local 一起用于测试")

    runtime = config.get("runtime", {}) or {}
    if not isinstance(runtime, Mapping):
        errors.append("runtime 必须是对象")
    else:
        lookback = runtime.get("lookback_days", 7)
        if not isinstance(lookback, int) or not 1 <= lookback <= 90:
            errors.append("runtime.lookback_days 必须为 1-90 的整数")
        for key, default in DEFAULT_BUDGETS.items():
            value = runtime.get(key, default)
            if not isinstance(value, int) or value <= 0:
                errors.append(f"runtime.{key} 必须为正整数")
            elif key == "max_runtime_seconds" and value > 86400:
                errors.append("runtime.max_runtime_seconds 不能超过 86400")


    if errors:
        raise ConfigError("; ".join(errors))
    return warnings


def redact_sensitive(value: Any) -> tuple[Any, list[str]]:
    flags: list[str] = []

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            output: dict[str, Any] = {}
            for key, item in node.items():
                if SECRET_KEY_RE.search(str(key)):
                    output[str(key)] = "[REDACTED]"
                    flags.append(f"敏感字段:{key}")
                else:
                    output[str(key)] = walk(item)
            return output
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str):
            cleaned = node
            for pattern in SECRET_VALUE_RES:
                if pattern.search(cleaned):
                    cleaned = pattern.sub("[REDACTED]", cleaned)
                    flags.append("疑似凭证")
            return cleaned
        return node

    return walk(value), sorted(set(flags))


def injection_flags(text: str) -> list[str]:
    lowered = text.lower()
    return [f"提示注入:{marker}" for marker in INJECTION_MARKERS if marker.lower() in lowered]


def parse_json_output(output: str) -> Any:
    output = output.strip()
    if not output:
        return {}
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise CommandError(f"命令未返回可解析 JSON: {output[:500]}")


def find_first_key(value: Any, keys: set[str]) -> Any:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in keys and item not in (None, ""):
                return item
        for item in value.values():
            found = find_first_key(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_first_key(item, keys)
            if found not in (None, ""):
                return found
    return None


def extract_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, Mapping):
        return []
    # lark-cli Base record-list returns a columnar shape:
    # {data: [[...]], fields: [...], record_id_list: [...]}
    rows = value.get("data")
    fields = value.get("fields")
    if isinstance(rows, list) and isinstance(fields, list):
        record_ids = value.get("record_id_list", [])
        records = []
        for index, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            records.append(
                {
                    "record_id": str(record_ids[index]) if index < len(record_ids) else "",
                    "fields": {str(name): row[position] if position < len(row) else None for position, name in enumerate(fields)},
                }
            )
        return records
    for wrapper in ("data", "result"):
        if isinstance(value.get(wrapper), Mapping):
            nested = extract_items(value[wrapper])
            if nested:
                return nested
    for key in ("items", "records", "results", "tables", "fields", "list", "notes", "recordings"):
        if isinstance(value.get(key), list):
            return list(value[key])
    return []


class ToolRunner:
    def __init__(self, config: Mapping[str, Any] | None = None):
        runtime = dict((config or {}).get("runtime", {}) or {})
        self.lark_cli = self._resolve_lark(runtime.get("lark_cli_path"))
        self.meegle_cli = self._resolve_meegle(runtime.get("meegle_cli_path"))

    @staticmethod
    def _resolve_lark(configured: Any) -> str:
        candidates = [
            str(configured or ""),
            shutil.which("lark-cli") or "",
            str(Path.home() / ".codex" / "skills" / "lark-shared" / "lark-cli.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        raise CommandError("找不到 lark-cli；请安装或配置 runtime.lark_cli_path")

    @staticmethod
    def _resolve_meegle(configured: Any) -> str | None:
        candidates = [str(configured or ""), shutil.which("meegle") or "", shutil.which("meegle.exe") or ""]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return None

    @staticmethod
    def _run(command: Sequence[str], timeout: int = 120) -> Any:
        process = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        if process.returncode != 0:
            message = (process.stderr or process.stdout).strip()
            raise CommandError(f"命令失败({process.returncode}): {message[:1200]}")
        return parse_json_output(process.stdout)

    def lark(self, args: Sequence[str], timeout: int = 120) -> Any:
        command = [self.lark_cli, *args]
        # lark-cli v7 shortcuts already emit JSON. Unlike meegle, lark-cli does
        # not expose a global --format flag, so appending it breaks commands.
        return self._run(command, timeout)

    def meegle(self, args: Sequence[str], timeout: int = 120) -> Any:
        if not self.meegle_cli:
            raise CommandError("找不到 meegle CLI；请先安装并配置 runtime.meegle_cli_path")
        command = [self.meegle_cli, *args]
        if "--format" not in command:
            command.extend(["--format", "json"])
        return self._run(command, timeout)


@dataclass
class RunBudget:
    max_metadata_items: int
    max_fulltext_items: int
    max_fulltext_chars: int
    max_candidates: int
    max_proposals: int
    max_runtime_seconds: int
    max_source_api_calls: int
    started_monotonic: float = field(default_factory=time.monotonic)
    metadata_items: int = 0
    fulltext_items: int = 0
    fulltext_chars: int = 0
    candidates: int = 0
    proposals: int = 0
    source_api_calls: int = 0
    discarded_fulltext_items: int = 0
    intake_stop_reason: str = ""
    processing_stop_reason: str = ""

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "RunBudget":
        runtime = config.get("runtime", {}) or {}
        return cls(**{key: int(runtime.get(key, default)) for key, default in DEFAULT_BUDGETS.items()})

    def elapsed_seconds(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 3)

    def _time_available(self) -> bool:
        if time.monotonic() - self.started_monotonic < self.max_runtime_seconds:
            return True
        reason = f"达到运行时长预算 {self.max_runtime_seconds} 秒"
        self.intake_stop_reason = self.intake_stop_reason or reason
        self.processing_stop_reason = self.processing_stop_reason or reason
        return False

    def _stop_intake(self, reason: str) -> None:
        self.intake_stop_reason = self.intake_stop_reason or reason

    def _stop_processing(self, reason: str) -> None:
        self.processing_stop_reason = self.processing_stop_reason or reason

    def claim_source_api(self) -> None:
        if not self._time_available():
            raise BudgetExceeded(self.stop_reason)
        if self.intake_stop_reason:
            raise BudgetExceeded(self.stop_reason)
        if self.source_api_calls >= self.max_source_api_calls:
            self._stop_intake(f"达到来源 API 调用预算 {self.max_source_api_calls} 次")
            raise BudgetExceeded(self.stop_reason)
        self.source_api_calls += 1

    def claim_metadata(self, amount: int = 1) -> bool:
        if not self._time_available() or self.intake_stop_reason:
            return False
        if self.metadata_items + amount > self.max_metadata_items:
            self._stop_intake(f"达到元数据预算 {self.max_metadata_items} 条")
            return False
        self.metadata_items += amount
        return True

    def allow_fulltext(self) -> bool:
        if not self._time_available() or self.intake_stop_reason:
            return False
        if self.fulltext_items >= self.max_fulltext_items:
            self._stop_intake(f"达到正文预算 {self.max_fulltext_items} 条")
            return False
        return True

    def record_fulltext(self, value: Any) -> bool:
        text_chars = len(json_text(value))
        self.fulltext_items += 1
        self.fulltext_chars += text_chars
        if self.fulltext_chars > self.max_fulltext_chars:
            self.discarded_fulltext_items += 1
            self._stop_intake(f"达到正文字符预算 {self.max_fulltext_chars} 字符")
            return False
        return self._time_available()

    def claim_candidate(self) -> bool:
        if not self._time_available() or self.processing_stop_reason:
            return False
        if self.candidates >= self.max_candidates:
            self._stop_processing(f"达到候选预算 {self.max_candidates} 条")
            return False
        self.candidates += 1
        return True

    def claim_proposal(self) -> bool:
        if not self._time_available() or self.processing_stop_reason:
            return False
        if self.proposals >= self.max_proposals:
            self._stop_processing(f"达到提案预算 {self.max_proposals} 条")
            return False
        self.proposals += 1
        return True

    @property
    def stop_reason(self) -> str:
        reasons = [reason for reason in (self.intake_stop_reason, self.processing_stop_reason) if reason]
        return "; ".join(dict.fromkeys(reasons))

    def counts(self) -> dict[str, Any]:
        return {
            "metadata_items": self.metadata_items,
            "fulltext_items": self.fulltext_items,
            "fulltext_chars": self.fulltext_chars,
            "discarded_fulltext_items": self.discarded_fulltext_items,
            "candidates": self.candidates,
            "proposal_budget_used": self.proposals,
            "source_api_calls": self.source_api_calls,
            "elapsed_seconds": self.elapsed_seconds(),
        }


class BudgetedRunner:
    def __init__(self, delegate: Any, budget: RunBudget):
        self.delegate = delegate
        self.budget = budget

    def lark(self, args: Sequence[str], timeout: int = 120) -> Any:
        self.budget.claim_source_api()
        return self.delegate.lark(args, timeout)

    def meegle(self, args: Sequence[str], timeout: int = 120) -> Any:
        self.budget.claim_source_api()
        return self.delegate.meegle(args, timeout)


@dataclass
class NormalizedRecord:
    source_type: str
    source_id: str
    source_url: str
    title: str
    owner: str
    source_owner: str
    business_purpose: str
    admission_type: str
    authority_level: str
    updated_at: str
    collected_at: str
    content_hash: str
    content: Any
    target_scope: str = "private"
    knowledge_mode: str = "stable"
    batch_key: str = ""


@dataclass
class Proposal:
    proposal_id: str
    agent_id: str
    title: str
    change_type: str
    target_scope: str
    target_document_token: str
    knowledge_content: str
    source_fact: str
    agent_inference: str
    source_type: str
    source_id: str
    source_url: str
    source_time: str
    collected_at: str
    content_hash: str
    relevance_reason: str
    risk_level: str
    security_flags: list[str]
    review_status: str = "待审核"
    reviewer: str = ""
    reviewed_at: str = ""
    applied_at: str = ""
    error: str = ""


def normalize_record(raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> NormalizedRecord:
    source_type = str(raw.get("source_type") or defaults.get("source_type") or "unknown")
    source_id = str(raw.get("source_id") or raw.get("id") or raw.get("record_id") or uuid.uuid4())
    sanitized, security = redact_sensitive(raw.get("content", raw))
    serialized = json_text(sanitized)
    flags = sorted(set(security + injection_flags(serialized)))
    content: Any = sanitized
    if flags:
        content = {"data": sanitized, "security_flags": flags}
    return NormalizedRecord(
        source_type=source_type,
        source_id=source_id,
        source_url=str(raw.get("source_url") or raw.get("url") or ""),
        title=str(raw.get("title") or raw.get("name") or f"{source_type}:{source_id}"),
        owner=str(raw.get("owner") or raw.get("owner_id") or ""),
        source_owner=str(raw.get("source_owner") or defaults.get("source_owner") or ""),
        business_purpose=str(raw.get("business_purpose") or defaults.get("business_purpose") or ""),
        admission_type=str(raw.get("admission_type") or defaults.get("admission_type") or ""),
        authority_level=str(raw.get("authority_level") or defaults.get("authority_level") or "reference"),
        updated_at=str(raw.get("updated_at") or raw.get("update_time") or raw.get("modified_time") or utc_now()),
        collected_at=str(raw.get("collected_at") or utc_now()),
        content_hash=sha256_text(serialized),
        content=content,
        target_scope=str(raw.get("target_scope") or defaults.get("target_scope") or "private"),
        knowledge_mode=str(raw.get("knowledge_mode") or defaults.get("knowledge_mode") or "stable"),
        batch_key=str(raw.get("batch_key") or defaults.get("batch_key") or f"{source_type}:default"),
    )


def relevance(record: NormalizedRecord, config: Mapping[str, Any]) -> tuple[bool, str]:
    rules = config.get("relevance", {}) or {}
    text = " ".join(
        [record.title, record.owner, record.source_url, json_text(record.content)]
    ).lower()
    excludes = [str(item).lower() for item in rules.get("exclude_keywords", []) or [] if str(item).strip()]
    for item in excludes:
        if item in text:
            return False, f"命中排除词:{item}"
    candidates: list[tuple[str, str]] = []
    for label, key in (
        ("关键词", "include_keywords"),
        ("人员", "related_people"),
        ("项目", "related_projects"),
    ):
        for item in rules.get(key, []) or []:
            item_text = str(item).strip().lower()
            if item_text:
                candidates.append((label, item_text))
    hits = [f"{label}:{item}" for label, item in candidates if item in text]
    if candidates and not hits:
        return False, "未命中任何相关性规则"
    if hits:
        return True, ";".join(hits)
    return True, "显式数据源白名单"


class Store:
    def put_proposal(self, proposal: Proposal) -> bool:
        raise NotImplementedError

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def update_proposal(self, proposal_id: str, fields: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def put_run(self, run: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def get_cursor(self, cursor_key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def put_cursor(self, cursor: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        raise NotImplementedError


class LocalStore(Store):
    def __init__(self, config_path: Path, agent_id: str):
        self.root = config_path.parent / ".agent-sync" / agent_id
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self, name: str, default: Any) -> Any:
        path = self.root / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, name: str, value: Any) -> None:
        path = self.root / name
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def put_proposal(self, proposal: Proposal) -> bool:
        proposals = self._read("proposals.json", {})
        if proposal.proposal_id in proposals:
            return False
        proposals[proposal.proposal_id] = asdict(proposal)
        self._write("proposals.json", proposals)
        return True

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return self._read("proposals.json", {}).get(proposal_id)

    def update_proposal(self, proposal_id: str, fields: Mapping[str, Any]) -> None:
        proposals = self._read("proposals.json", {})
        if proposal_id not in proposals:
            raise SyncError(f"提案不存在: {proposal_id}")
        proposals[proposal_id].update(dict(fields))
        self._write("proposals.json", proposals)

    def put_run(self, run: Mapping[str, Any]) -> None:
        runs = self._read("runs.json", [])
        for index, existing in enumerate(runs):
            if existing.get("run_id") == run.get("run_id"):
                runs[index] = dict(run)
                break
        else:
            runs.append(dict(run))
        self._write("runs.json", runs[-200:])

    def get_cursor(self, cursor_key: str) -> dict[str, Any] | None:
        return self._read("cursors.json", {}).get(cursor_key)

    def put_cursor(self, cursor: Mapping[str, Any]) -> None:
        cursors = self._read("cursors.json", {})
        cursors[str(cursor["cursor_key"])] = dict(cursor)
        self._write("cursors.json", cursors)

    def status(self) -> dict[str, Any]:
        proposals = self._read("proposals.json", {})
        runs = self._read("runs.json", [])
        cursors = self._read("cursors.json", {})
        states: dict[str, int] = {}
        for proposal in proposals.values():
            state = proposal.get("review_status", "未知")
            states[state] = states.get(state, 0) + 1
        return {
            "storage": "local",
            "root": str(self.root),
            "proposal_states": states,
            "cursor_count": len(cursors),
            "last_run": runs[-1] if runs else None,
        }


PROPOSAL_FIELDS = {
    "提案ID",
    "AgentID",
    "标题",
    "变更类型",
    "目标范围",
    "目标文档Token",
    "知识正文",
    "原文事实",
    "Agent推断",
    "来源类型",
    "来源ID",
    "来源链接",
    "来源时间",
    "采集时间",
    "内容哈希",
    "相关性理由",
    "风险等级",
    "安全标记",
    "审核状态",
    "审核人",
    "审核时间",
    "应用时间",
    "错误信息",
}
RUN_FIELDS = {"运行ID", "AgentID", "开始时间", "结束时间", "运行状态", "统计", "错误摘要"}
CURSOR_FIELDS = {"游标键", "AgentID", "来源类型", "范围ID", "最后更新时间", "批次哈希", "成功运行ID"}


class BaseStore(Store):
    def __init__(self, config: Mapping[str, Any], runner: ToolRunner):
        review = config["review"]
        self.base_token = str(review["base_token"])
        self.tables = dict(review["tables"])
        self.runner = runner
        self._validate_fields("proposals", PROPOSAL_FIELDS)
        self._validate_fields("runs", RUN_FIELDS)
        self._validate_fields("cursors", CURSOR_FIELDS)

    def _validate_fields(self, table_key: str, required: set[str]) -> None:
        data = self.runner.lark(
            ["base", "+field-list", "--base-token", self.base_token, "--table-id", str(self.tables[table_key]), "--limit", "200"]
        )
        names = {
            str(item.get("field_name") or item.get("name") or "")
            for item in extract_items(data)
            if isinstance(item, Mapping)
        }
        missing = required - names
        if missing:
            raise ConfigError(f"{table_key} 表缺少字段: {', '.join(sorted(missing))}")

    def _list_records(self, table_key: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0
        for _ in range(20):
            data = self.runner.lark(
                [
                    "base",
                    "+record-list",
                    "--base-token",
                    self.base_token,
                    "--table-id",
                    str(self.tables[table_key]),
                    "--offset",
                    str(offset),
                    "--limit",
                    "200",
                ]
            )
            page = [item for item in extract_items(data) if isinstance(item, Mapping)]
            records.extend(dict(item) for item in page)
            has_more = bool(find_first_key(data, {"has_more"}))
            if not has_more or not page:
                break
            offset += len(page)
        return records

    @staticmethod
    def _fields(record: Mapping[str, Any]) -> dict[str, Any]:
        fields = record.get("fields")
        raw = dict(fields) if isinstance(fields, Mapping) else dict(record)
        return {
            key: (value[0] if isinstance(value, list) and len(value) == 1 else value)
            for key, value in raw.items()
        }

    @staticmethod
    def _record_id(record: Mapping[str, Any]) -> str:
        return str(record.get("record_id") or record.get("id") or "")

    def _find(self, table_key: str, field: str, value: str) -> dict[str, Any] | None:
        for record in self._list_records(table_key):
            if str(self._fields(record).get(field, "")) == value:
                return record
        return None

    def _upsert(self, table_key: str, unique_field: str, unique_value: str, values: Mapping[str, Any]) -> bool:
        existing = self._find(table_key, unique_field, unique_value)
        args = [
            "base",
            "+record-upsert",
            "--base-token",
            self.base_token,
            "--table-id",
            str(self.tables[table_key]),
        ]
        if existing:
            args.extend(["--record-id", self._record_id(existing)])
        args.extend(["--json", json_text(values)])
        self.runner.lark(args)
        return existing is None

    def put_proposal(self, proposal: Proposal) -> bool:
        if self._find("proposals", "提案ID", proposal.proposal_id):
            return False
        values = {
            "提案ID": proposal.proposal_id,
            "AgentID": proposal.agent_id,
            "标题": proposal.title,
            "变更类型": proposal.change_type,
            "目标范围": proposal.target_scope,
            "目标文档Token": proposal.target_document_token,
            "知识正文": proposal.knowledge_content,
            "原文事实": proposal.source_fact,
            "Agent推断": proposal.agent_inference,
            "来源类型": proposal.source_type,
            "来源ID": proposal.source_id,
            "来源链接": proposal.source_url,
            "来源时间": proposal.source_time,
            "采集时间": proposal.collected_at,
            "内容哈希": proposal.content_hash,
            "相关性理由": proposal.relevance_reason,
            "风险等级": proposal.risk_level,
            "安全标记": ";".join(proposal.security_flags),
            "审核状态": proposal.review_status,
            "审核人": proposal.reviewer,
            "审核时间": proposal.reviewed_at,
            "应用时间": proposal.applied_at,
            "错误信息": proposal.error,
        }
        return self._upsert("proposals", "提案ID", proposal.proposal_id, values)

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        record = self._find("proposals", "提案ID", proposal_id)
        if not record:
            return None
        fields = self._fields(record)
        return {
            "proposal_id": fields.get("提案ID", ""),
            "agent_id": fields.get("AgentID", ""),
            "title": fields.get("标题", ""),
            "change_type": fields.get("变更类型", ""),
            "target_scope": fields.get("目标范围", ""),
            "target_document_token": fields.get("目标文档Token", ""),
            "knowledge_content": fields.get("知识正文", ""),
            "source_fact": fields.get("原文事实", ""),
            "agent_inference": fields.get("Agent推断", ""),
            "source_type": fields.get("来源类型", ""),
            "source_id": fields.get("来源ID", ""),
            "source_url": fields.get("来源链接", ""),
            "source_time": fields.get("来源时间", ""),
            "collected_at": fields.get("采集时间", ""),
            "content_hash": fields.get("内容哈希", ""),
            "relevance_reason": fields.get("相关性理由", ""),
            "risk_level": fields.get("风险等级", ""),
            "security_flags": str(fields.get("安全标记", "")).split(";") if fields.get("安全标记") else [],
            "review_status": fields.get("审核状态", ""),
            "reviewer": fields.get("审核人", ""),
            "reviewed_at": fields.get("审核时间", ""),
            "applied_at": fields.get("应用时间", ""),
            "error": fields.get("错误信息", ""),
        }

    def update_proposal(self, proposal_id: str, fields: Mapping[str, Any]) -> None:
        existing = self._find("proposals", "提案ID", proposal_id)
        if not existing:
            raise SyncError(f"提案不存在: {proposal_id}")
        field_map = {
            "review_status": "审核状态",
            "reviewer": "审核人",
            "reviewed_at": "审核时间",
            "applied_at": "应用时间",
            "error": "错误信息",
        }
        values = {field_map.get(key, key): value for key, value in fields.items()}
        args = [
            "base",
            "+record-upsert",
            "--base-token",
            self.base_token,
            "--table-id",
            str(self.tables["proposals"]),
            "--record-id",
            self._record_id(existing),
            "--json",
            json_text(values),
        ]
        self.runner.lark(args)

    def put_run(self, run: Mapping[str, Any]) -> None:
        values = {
            "运行ID": run["run_id"],
            "AgentID": run["agent_id"],
            "开始时间": run["started_at"],
            "结束时间": run["ended_at"],
            "运行状态": run["status"],
            "统计": json_text(run.get("counts", {})),
            "错误摘要": json_text(run.get("errors", [])),
        }
        self._upsert("runs", "运行ID", str(run["run_id"]), values)

    def get_cursor(self, cursor_key: str) -> dict[str, Any] | None:
        record = self._find("cursors", "游标键", cursor_key)
        if not record:
            return None
        fields = self._fields(record)
        return {
            "cursor_key": fields.get("游标键", ""),
            "agent_id": fields.get("AgentID", ""),
            "source_type": fields.get("来源类型", ""),
            "scope_id": fields.get("范围ID", ""),
            "last_updated": fields.get("最后更新时间", ""),
            "batch_hash": fields.get("批次哈希", ""),
            "last_success_run_id": fields.get("成功运行ID", ""),
        }

    def put_cursor(self, cursor: Mapping[str, Any]) -> None:
        values = {
            "游标键": cursor["cursor_key"],
            "AgentID": cursor["agent_id"],
            "来源类型": cursor["source_type"],
            "范围ID": cursor["scope_id"],
            "最后更新时间": cursor["last_updated"],
            "批次哈希": cursor["batch_hash"],
            "成功运行ID": cursor["last_success_run_id"],
        }
        self._upsert("cursors", "游标键", str(cursor["cursor_key"]), values)

    def status(self) -> dict[str, Any]:
        proposals = self._list_records("proposals")
        runs = self._list_records("runs")
        cursors = self._list_records("cursors")
        states: dict[str, int] = {}
        for record in proposals:
            state = str(self._fields(record).get("审核状态", "未知"))
            states[state] = states.get(state, 0) + 1
        return {
            "storage": "base",
            "base_token": self.base_token,
            "proposal_states": states,
            "cursor_count": len(cursors),
            "run_count": len(runs),
        }


def make_store(config: Mapping[str, Any], config_path: Path, runner: ToolRunner) -> Store:
    if (config.get("review", {}) or {}).get("storage", "local") == "base":
        return BaseStore(config, runner)
    return LocalStore(config_path, str(config["agent"]["id"]))


def _date_range(cursor: Mapping[str, Any] | None, lookback_days: int) -> tuple[str, str]:
    end = datetime.now().astimezone()
    if cursor and cursor.get("last_updated"):
        try:
            start = datetime.fromisoformat(str(cursor["last_updated"]).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=end.tzinfo)
        except ValueError:
            start = end - timedelta(days=lookback_days)
    else:
        start = end - timedelta(days=lookback_days)
    return start.date().isoformat(), end.date().isoformat()


def _source_policy_defaults(source: Mapping[str, Any]) -> dict[str, str]:
    return {
        "source_owner": str(source.get("source_owner", "")),
        "business_purpose": str(source.get("business_purpose", "")),
        "admission_type": str(source.get("admission_type", "")),
        "authority_level": str(source.get("authority_level", "reference")),
    }


def _raw_record(item: Mapping[str, Any], defaults: Mapping[str, Any]) -> NormalizedRecord:
    raw = {
        "source_type": defaults["source_type"],
        "source_id": item.get("source_id") or item.get("record_id") or item.get("id") or item.get("token"),
        "source_url": item.get("url") or item.get("source_url") or "",
        "title": item.get("title") or item.get("name") or defaults.get("title") or "",
        "owner": item.get("owner") or item.get("owner_id") or item.get("creator_id") or "",
        "updated_at": item.get("updated_at") or item.get("update_time") or item.get("modified_time") or utc_now(),
        "content": item.get("content", item),
        "target_scope": defaults.get("target_scope", "private"),
        "knowledge_mode": defaults.get("knowledge_mode", "stable"),
        "batch_key": defaults["batch_key"],
    }
    return normalize_record(raw, defaults)


def collect_fixture_batches(config: Mapping[str, Any], config_path: Path, budget: RunBudget) -> list[list[NormalizedRecord]]:
    batches: dict[str, list[NormalizedRecord]] = {}
    for configured in (config.get("sources", {}) or {}).get("fixtures", []) or []:
        if budget.intake_stop_reason:
            break
        fixture_path = Path(str(configured))
        if not fixture_path.is_absolute():
            fixture_path = config_path.parent / fixture_path
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else payload.get("items", [])
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            if not budget.claim_metadata():
                break
            defaults = {
                "source_type": raw.get("source_type", "fixture"),
                "source_owner": str(config.get("agent", {}).get("owner", "test")),
                "business_purpose": "isolated test fixture",
                "admission_type": "test_fixture",
                "authority_level": "reference",
                "batch_key": raw.get("batch_key", f"fixture:{fixture_path.name}"),
                "target_scope": raw.get("target_scope", "private"),
                "knowledge_mode": raw.get("knowledge_mode", "stable"),
            }
            record = normalize_record(raw, defaults)
            if record.knowledge_mode == "stable":
                if not budget.allow_fulltext() or not budget.record_fulltext(record.content):
                    break
            batches.setdefault(record.batch_key, []).append(record)
    return list(batches.values())


def collect_meeting_batches(config: Mapping[str, Any], store: Store, runner: Any, budget: RunBudget) -> list[list[NormalizedRecord]]:
    source = (config.get("sources", {}) or {}).get("meetings", {}) or {}
    if not isinstance(source, Mapping):
        return []
    if str(source.get("knowledge_mode", "stable")) != "stable":
        return []
    output: list[list[NormalizedRecord]] = []
    defaults_base = {
        "source_type": "meeting",
        **_source_policy_defaults(source),
        "target_scope": source.get("target_scope", "private"),
        "knowledge_mode": source.get("knowledge_mode", "stable"),
    }
    lookback = int((config.get("runtime", {}) or {}).get("lookback_days", 7))
    participant_ids = [str(item) for item in source.get("participant_ids", []) or []]
    for keyword in source.get("keywords", []) or []:
        if budget.intake_stop_reason:
            break
        batch_key = f"meeting:query:{keyword}"
        start, end = _date_range(store.get_cursor(batch_key), lookback)
        args = ["vc", "+search", "--query", str(keyword), "--start", start, "--end", end, "--page-size", "30"]
        if participant_ids:
            args.extend(["--participant-ids", ",".join(participant_ids)])
        search = runner.lark(args)
        batch: list[NormalizedRecord] = []
        for meeting in extract_items(search):
            if not isinstance(meeting, Mapping):
                continue
            meeting_id = str(meeting.get("id") or meeting.get("meeting_id") or "")
            if not meeting_id:
                continue
            if not budget.claim_metadata() or not budget.allow_fulltext():
                break
            content: dict[str, Any] = {"meeting": meeting}
            try:
                recording = runner.lark(["vc", "+recording", "--meeting-ids", meeting_id])
                content["recording"] = recording
                minute_token = find_first_key(recording, {"minute_token"})
                if minute_token:
                    content["notes"] = runner.lark(["vc", "+notes", "--minute-tokens", str(minute_token)])
                else:
                    content["notes"] = runner.lark(["vc", "+notes", "--meeting-ids", meeting_id])
            except CommandError as exc:
                content["notes_error"] = str(exc)
            if not budget.record_fulltext(content):
                break
            item = dict(meeting)
            item["content"] = content
            item["source_id"] = meeting_id
            defaults = dict(defaults_base, batch_key=batch_key, title=str(meeting.get("topic") or keyword))
            batch.append(_raw_record(item, defaults))
        output.append(batch)
    for keyword in source.get("minutes_keywords", []) or []:
        if budget.intake_stop_reason:
            break
        batch_key = f"minutes:query:{keyword}"
        start, end = _date_range(store.get_cursor(batch_key), lookback)
        args = ["minutes", "+search", "--query", str(keyword), "--start", start, "--end", end, "--page-size", "50"]
        if participant_ids:
            args.extend(["--participant-ids", ",".join(participant_ids)])
        owner_ids = [str(item) for item in source.get("owner_ids", []) or []]
        if owner_ids:
            args.extend(["--owner-ids", ",".join(owner_ids)])
        result = runner.lark(args)
        batch = []
        for minute in extract_items(result):
            if not isinstance(minute, Mapping):
                continue
            minute_token = str(minute.get("minute_token") or minute.get("token") or minute.get("id") or "")
            if not minute_token:
                continue
            if not budget.claim_metadata() or not budget.allow_fulltext():
                break
            notes = runner.lark(["vc", "+notes", "--minute-tokens", minute_token])
            raw = dict(minute)
            raw.update(
                {
                    "source_id": minute_token,
                    "source_url": minute.get("url") or f"https://feishu.cn/minutes/{minute_token}",
                    "title": minute.get("title") or str(keyword),
                    "content": {"minute": minute, "notes": notes},
                }
            )
            if not budget.record_fulltext(raw["content"]):
                break
            batch.append(_raw_record(raw, dict(defaults_base, batch_key=batch_key)))
        output.append(batch)
    for minute_token in source.get("minute_tokens", []) or []:
        if budget.intake_stop_reason:
            break
        if not budget.claim_metadata() or not budget.allow_fulltext():
            break
        batch_key = f"meeting:minute:{minute_token}"
        notes = runner.lark(["vc", "+notes", "--minute-tokens", str(minute_token)])
        raw = {
            "source_id": str(minute_token),
            "title": str(find_first_key(notes, {"title"}) or f"妙记 {minute_token}"),
            "source_url": f"https://feishu.cn/minutes/{minute_token}",
            "content": notes,
        }
        if not budget.record_fulltext(notes):
            break
        output.append([normalize_record(raw, dict(defaults_base, batch_key=batch_key))])
    return output


def collect_document_batches(config: Mapping[str, Any], runner: Any, budget: RunBudget) -> list[list[NormalizedRecord]]:
    source = (config.get("sources", {}) or {}).get("documents", {}) or {}
    if not isinstance(source, Mapping):
        return []
    if str(source.get("knowledge_mode", "stable")) != "stable":
        return []
    output: list[list[NormalizedRecord]] = []
    defaults_base = {
        "source_type": "document",
        **_source_policy_defaults(source),
        "target_scope": source.get("target_scope", "private"),
        "knowledge_mode": source.get("knowledge_mode", "stable"),
    }
    seen: set[str] = set()

    def fetch(token: str, batch_key: str, metadata_claimed: bool = False) -> bool:
        if token in seen:
            return True
        seen.add(token)
        if not metadata_claimed and not budget.claim_metadata():
            return False
        if not budget.allow_fulltext():
            return False
        data = runner.lark(["docs", "+fetch", "--doc", token])
        payload = data.get("data", data) if isinstance(data, Mapping) else data
        if not budget.record_fulltext(payload):
            return False
        raw = {
            "source_id": token,
            "title": find_first_key(payload, {"title", "name"}) or f"文档 {token}",
            "source_url": find_first_key(payload, {"url"}) or "",
            "updated_at": find_first_key(payload, {"updated_at", "update_time", "modified_time"}) or utc_now(),
            "content": payload,
        }
        output.append([normalize_record(raw, dict(defaults_base, batch_key=batch_key))])

        return True
    for token in source.get("document_tokens", []) or []:
        if not fetch(str(token), f"document:token:{token}"):
            break

    folders = [str(item) for item in source.get("folder_tokens", []) or []]
    for keyword in source.get("search_keywords", []) or []:
        filter_body = {"folder_tokens": folders} if folders else {}
        args = ["docs", "+search", "--query", str(keyword), "--page-size", "20"]
        if budget.intake_stop_reason:
            break
        if filter_body:
            args.extend(["--filter", json_text(filter_body)])
        result = runner.lark(args)
        for item in extract_items(result):
            if not isinstance(item, Mapping):
                continue
            if not budget.claim_metadata():
                break
            token = str(item.get("token") or item.get("file_token") or item.get("obj_token") or "")
            if token and not fetch(token, f"document:search:{keyword}", metadata_claimed=True):
                break
    return output


def collect_base_batches(config: Mapping[str, Any], runner: Any, budget: RunBudget) -> list[list[NormalizedRecord]]:
    output: list[list[NormalizedRecord]] = []
    for source in (config.get("sources", {}) or {}).get("bases", []) or []:
        if budget.intake_stop_reason:
            break
        if isinstance(source, Mapping) and str(source.get("knowledge_mode", "dynamic")) != "stable":
            continue
        if not isinstance(source, Mapping):
            continue
        base_token, table_id = str(source.get("base_token", "")), str(source.get("table_id", ""))
        if not base_token or not table_id:
            raise ConfigError("Base 来源必须配置 base_token 和 table_id")
        runner.lark(["base", "+field-list", "--base-token", base_token, "--table-id", table_id, "--limit", "200"])
        batch_key = f"base:{base_token}:{table_id}:{source.get('view_id', '')}"
        batch: list[NormalizedRecord] = []
        offset = 0
        for _ in range(int(source.get("max_pages", 5))):
            if budget.intake_stop_reason:
                break
            remaining_metadata = budget.max_metadata_items - budget.metadata_items
            if remaining_metadata <= 0:
                budget._stop_intake(f"达到元数据预算 {budget.max_metadata_items} 条")
                break
            page_limit = min(200, remaining_metadata)
            args = ["base", "+record-list", "--base-token", base_token, "--table-id", table_id, "--offset", str(offset), "--limit", str(page_limit)]
            if source.get("view_id"):
                args.extend(["--view-id", str(source["view_id"])])
            data = runner.lark(args)
            page = extract_items(data)
            defaults = {
                "source_type": "base",
                **_source_policy_defaults(source),
                "batch_key": batch_key,
                "target_scope": source.get("target_scope", "private"),
                "knowledge_mode": source.get("knowledge_mode", "dynamic"),
                "title": str(source.get("name") or table_id),
            }
            for item in page:
                if isinstance(item, Mapping):
                    record = dict(item)
                    if not budget.claim_metadata() or not budget.allow_fulltext():
                        break
                    record["source_id"] = f"{table_id}:{item.get('record_id') or item.get('id')}"
                    record["source_url"] = str(source.get("url", ""))
                    normalized = _raw_record(record, defaults)
                    if not budget.record_fulltext(normalized.content):
                        break
                    batch.append(normalized)
            if budget.intake_stop_reason:
                break
            if not bool(find_first_key(data, {"has_more"})) or not page:
                break
            offset += len(page)
        output.append(batch)
    return output


def collect_meegle_batches(config: Mapping[str, Any], runner: Any, budget: RunBudget) -> list[list[NormalizedRecord]]:
    sources = [source for source in (config.get("sources", {}) or {}).get("meegle", []) or [] if isinstance(source, Mapping) and str(source.get("knowledge_mode", "dynamic")) == "stable"]
    if not sources:
        return []
    status = runner.meegle(["auth", "status"])
    if not bool(find_first_key(status, {"authenticated"})):
        raise CommandError("Meegle 未登录；请先运行 meegle auth login --host project.feishu.cn")
    output: list[list[NormalizedRecord]] = []
    for source in sources:
        if budget.intake_stop_reason:
            break
        if not isinstance(source, Mapping):
            continue
        project_key = str(source.get("project_key", ""))
        if not project_key:
            raise ConfigError("Meegle 来源必须配置 project_key")
        defaults_base = {
            "source_type": "meegle",
            **_source_policy_defaults(source),
            "target_scope": source.get("target_scope", "private"),
            "knowledge_mode": source.get("knowledge_mode", "dynamic"),
        }
        for view_id in source.get("view_ids", []) or []:
            batch_key = f"meegle:{project_key}:view:{view_id}"
            batch: list[NormalizedRecord] = []
            for page_num in range(1, int(source.get("max_pages", 5)) + 1):
                if budget.intake_stop_reason:
                    break
                data = runner.meegle(["view", "get", "--project-key", project_key, "--view-id", str(view_id), "--page-num", str(page_num)])
                page = extract_items(data)
                if not page:
                    break
                for item in page:
                    if isinstance(item, Mapping):
                        if not budget.claim_metadata() or not budget.allow_fulltext():
                            break
                        record = dict(item)
                        record["source_id"] = f"{project_key}:{item.get('work_item_id') or item.get('id')}"
                        normalized = _raw_record(record, dict(defaults_base, batch_key=batch_key, title=str(view_id)))
                        if not budget.record_fulltext(normalized.content):
                            break
                        batch.append(normalized)
                if budget.intake_stop_reason:
                    break
            output.append(batch)
        for index, mql in enumerate(source.get("mql", []) or []):
            batch_key = f"meegle:{project_key}:mql:{index}"
            if budget.intake_stop_reason:
                break
            data = runner.meegle(["workitem", "query", "--project-key", project_key, "--mql", str(mql)])
            batch = []
            for item in extract_items(data):
                if isinstance(item, Mapping):
                    record = dict(item)
                    if not budget.claim_metadata() or not budget.allow_fulltext():
                        break
                    record["source_id"] = f"{project_key}:{item.get('work_item_id') or item.get('id')}"
                    normalized = _raw_record(record, dict(defaults_base, batch_key=batch_key, title=f"MQL {index + 1}"))
                    if not budget.record_fulltext(normalized.content):
                        break
                    batch.append(normalized)
            output.append(batch)
    return output


def proposal_for(record: NormalizedRecord, config: Mapping[str, Any], reason: str) -> Proposal:
    agent_id = str(config["agent"]["id"])
    proposal_id = sha256_text(f"{agent_id}|{record.source_type}|{record.source_id}|{record.content_hash}")[:24]
    knowledge = config.get("knowledge", {}) or {}
    if record.target_scope == "public":
        target = str(knowledge.get("public_document_token", ""))
        risk = "高"
    else:
        target = str(knowledge.get("private_document_token", ""))
        risk = "中"
    serialized = json_text(record.content)
    max_chars = int((config.get("relevance", {}) or {}).get("max_candidate_chars", 12000))
    candidate = serialized[:max_chars]
    flags = []
    if isinstance(record.content, Mapping):
        flags = list(record.content.get("security_flags", []) or [])
    if flags:
        risk = "高"
    governance_evidence = [
        f"准入:{record.admission_type}",
        f"权威:{record.authority_level}",
        f"来源负责人:{record.source_owner}",
        f"业务目的:{record.business_purpose}",
    ]
    reason = ";".join([reason, *governance_evidence])

    return Proposal(
        proposal_id=proposal_id,
        agent_id=agent_id,
        title=record.title,
        change_type="新增",
        target_scope=record.target_scope,
        target_document_token=target,
        knowledge_content=candidate,
        source_fact=candidate,
        agent_inference="",
        source_type=record.source_type,
        source_id=record.source_id,
        source_url=record.source_url,
        source_time=record.updated_at,
        collected_at=record.collected_at,
        content_hash=record.content_hash,
        relevance_reason=reason,
        risk_level=risk,
        security_flags=flags,
    )


def batch_hash(records: Iterable[NormalizedRecord]) -> str:
    return sha256_text("|".join(sorted(record.content_hash for record in records)))


def scan(config: Mapping[str, Any], config_path: Path, runner: ToolRunner, store: Store, dry_run: bool = False) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    started = utc_now()
    budget = RunBudget.from_config(config)
    bounded_runner = BudgetedRunner(runner, budget)
    pending_cursors: list[dict[str, Any]] = []
    preview_proposals: set[str] = set()
    counts = {"collected": 0, "dynamic": 0, "irrelevant": 0, "existing": 0, "proposed": 0, "would_propose": 0, "failed_batches": 0}
    errors: list[dict[str, str]] = []
    batch_groups: list[list[NormalizedRecord]] = []
    collectors = [
        ("fixtures", lambda: collect_fixture_batches(config, config_path, budget)),
        ("meetings", lambda: collect_meeting_batches(config, store, bounded_runner, budget)),
        ("documents", lambda: collect_document_batches(config, bounded_runner, budget)),
        ("bases", lambda: collect_base_batches(config, bounded_runner, budget)),
        ("meegle", lambda: collect_meegle_batches(config, bounded_runner, budget)),
    ]
    for name, collector in collectors:
        if budget.intake_stop_reason:
            break
        try:
            batch_groups.extend(collector())
        except BudgetExceeded:
            break
        except Exception as exc:  # isolate source families
            counts["failed_batches"] += 1
            errors.append({"source": name, "error": str(exc)})

    for records in batch_groups:
        if budget.processing_stop_reason:
            break
        if not records:
            continue
        batch_key = records[0].batch_key
        try:
            current_hash = batch_hash(records)
            cursor = store.get_cursor(batch_key)
            if cursor and cursor.get("batch_hash") == current_hash:
                counts["existing"] += len(records)
                continue
            for record in records:
                counts["collected"] += 1
                if record.knowledge_mode != "stable":
                    counts["dynamic"] += 1
                    continue
                accepted, reason = relevance(record, config)
                if not accepted:
                    counts["irrelevant"] += 1
                    continue
                if not budget.claim_candidate():
                    break
                proposal = proposal_for(record, config, reason)
                if dry_run:
                    if proposal.proposal_id in preview_proposals or store.get_proposal(proposal.proposal_id):
                        counts["existing"] += 1
                        continue
                    if not budget.claim_proposal():
                        break
                    preview_proposals.add(proposal.proposal_id)
                    counts["would_propose"] += 1
                else:
                    if not budget.claim_proposal():
                        break
                    if store.put_proposal(proposal):
                        counts["proposed"] += 1
                    else:
                        counts["existing"] += 1
            if budget.stop_reason:
                continue
            latest = max((record.updated_at for record in records), default=utc_now())
            pending_cursors.append(
                {
                    "cursor_key": batch_key,
                    "agent_id": config["agent"]["id"],
                    "source_type": records[0].source_type,
                    "scope_id": batch_key,
                    "last_updated": latest,
                    "batch_hash": current_hash,
                    "last_success_run_id": run_id,
                }
            )
        except Exception as exc:
            counts["failed_batches"] += 1
            errors.append({"source": batch_key, "error": str(exc)})

    counts.update(budget.counts())
    if budget.stop_reason:
        status = "partial"
    elif errors and counts["collected"]:
        status = "partial"
    elif errors:
        status = "failed"
    else:
        status = "success"
    run = {
        "run_id": run_id,
        "agent_id": config["agent"]["id"],
        "started_at": started,
        "ended_at": utc_now(),
        "status": status,
        "counts": counts,
        "errors": errors,
        "dry_run": dry_run,
        "budget_stop_reason": budget.stop_reason,
    }
    if not dry_run:
        store.put_run(run)
        for cursor in pending_cursors:
            try:
                store.put_cursor(cursor)
            except Exception as exc:
                counts["failed_batches"] += 1
                errors.append({"source": str(cursor["cursor_key"]), "error": f"游标提交失败: {exc}"})
                run["status"] = "partial" if counts["collected"] else "failed"
                run["ended_at"] = utc_now()
                store.put_run(run)
                break
    return run


def render_knowledge(proposal: Mapping[str, Any]) -> str:
    return (
        f"## {proposal.get('title') or '知识更新'}\n\n"
        f"> 同步标识：`{proposal['proposal_id']}`  \n"
        f"> 来源：{proposal.get('source_type', '')} / {proposal.get('source_url') or proposal.get('source_id', '')}  \n"
        f"> 来源时间：{proposal.get('source_time', '')}  \n"
        f"> 治理依据：{proposal.get('relevance_reason', '')}  \n"
        f"> 内容哈希：`{proposal.get('content_hash', '')}`\n\n"
        f"{proposal.get('knowledge_content', '')}\n"
    )


def apply_proposal(
    config: Mapping[str, Any],
    proposal_id: str,
    runner: ToolRunner,
    store: Store,
    dry_run: bool = False,
) -> dict[str, Any]:
    proposal = store.get_proposal(proposal_id)
    if not proposal:
        raise SyncError(f"提案不存在: {proposal_id}")
    if proposal.get("agent_id") != config["agent"]["id"]:
        raise SyncError("提案所属 Agent 与配置不一致")
    state = proposal.get("review_status")
    if state == "已应用":
        return {"proposal_id": proposal_id, "status": "already_applied"}
    if state != "已批准":
        raise SyncError(f"提案状态必须为已批准，当前为: {state}")
    expected = (
        config["knowledge"].get("public_document_token")
        if proposal.get("target_scope") == "public"
        else config["knowledge"].get("private_document_token")
    )
    target = str(proposal.get("target_document_token") or "")
    if not expected or str(expected) in PLACEHOLDERS or target != str(expected):
        raise SyncError("提案目标文档与配置不一致或尚未配置")
    markdown = render_knowledge(proposal)
    if dry_run:
        return {"proposal_id": proposal_id, "status": "dry_run", "target": target, "markdown": markdown}
    try:
        existing = runner.lark(["docs", "+fetch", "--doc", target])
        if proposal_id not in json_text(existing):
            runner.lark(["docs", "+update", "--doc", target, "--mode", "append", "--markdown", markdown])
        store.update_proposal(proposal_id, {"review_status": "已应用", "applied_at": utc_now(), "error": ""})
        return {"proposal_id": proposal_id, "status": "applied", "target": target}
    except Exception as exc:
        store.update_proposal(proposal_id, {"error": str(exc)})
        raise


def field_text(name: str) -> dict[str, Any]:
    return {"name": name, "type": "text"}


def field_select(name: str, options: Sequence[str]) -> dict[str, Any]:
    return {
        "name": name,
        "type": "select",
        "multiple": False,
        "options": [{"name": option} for option in options],
    }


def bootstrap_base(runner: ToolRunner, name: str, existing_base_token: str = "") -> dict[str, Any]:
    if existing_base_token:
        base_token = existing_base_token
        base_url = f"https://feishu.cn/base/{base_token}"
    else:
        created = runner.lark(["base", "+base-create", "--name", name, "--time-zone", "Asia/Shanghai"])
        base_token = find_first_key(created, {"base_token", "app_token", "token"})
        if not base_token:
            raise SyncError("Base 创建成功响应中未找到 Base Token")
        base_url = find_first_key(created, {"url"}) or ""
    schemas: list[tuple[str, list[dict[str, Any]]]] = [
        (
            "知识变更审核表",
            [
                field_text("提案ID"), field_text("AgentID"), field_text("标题"),
                field_select("变更类型", ["新增", "替换", "过期", "冲突", "删除"]),
                field_select("目标范围", ["public", "private"]), field_text("目标文档Token"),
                field_text("知识正文"), field_text("原文事实"), field_text("Agent推断"),
                field_select("来源类型", ["meeting", "document", "base", "meegle", "fixture"]),
                field_text("来源ID"), field_text("来源链接"), field_text("来源时间"), field_text("采集时间"),
                field_text("内容哈希"), field_text("相关性理由"), field_select("风险等级", ["低", "中", "高"]),
                field_text("安全标记"), field_select("审核状态", sorted(APPROVAL_STATES)), field_text("审核人"),
                field_text("审核时间"), field_text("应用时间"), field_text("错误信息"),
            ],
        ),
        (
            "同步运行表",
            [field_text("运行ID"), field_text("AgentID"), field_text("开始时间"), field_text("结束时间"),
             field_select("运行状态", ["success", "partial", "failed"]), field_text("统计"), field_text("错误摘要")],
        ),
        (
            "同步游标表",
            [field_text("游标键"), field_text("AgentID"), field_select("来源类型", ["meeting", "document", "base", "meegle", "fixture"]),
             field_text("范围ID"), field_text("最后更新时间"), field_text("批次哈希"), field_text("成功运行ID")],
        ),
    ]
    existing_tables = runner.lark(["base", "+table-list", "--base-token", str(base_token), "--limit", "100"])
    existing_by_name: dict[str, str] = {}
    for item in extract_items(existing_tables):
        if isinstance(item, Mapping) and item.get("name") and (item.get("table_id") or item.get("id")):
            existing_by_name[str(item["name"])] = str(item.get("table_id") or item.get("id"))
    table_ids: dict[str, str] = {}
    for table_name, fields in schemas:
        if table_name in existing_by_name:
            table_ids[table_name] = existing_by_name[table_name]
            continue
        response = runner.lark(
            ["base", "+table-create", "--base-token", str(base_token), "--name", table_name, "--fields", json_text(fields)]
        )
        table_id = find_first_key(response, {"table_id", "id"})
        if not table_id:
            raise SyncError(f"{table_name} 创建响应中未找到 table_id")
        table_ids[table_name] = str(table_id)
    # Some lark-cli table-create responses expose nested field `id` values
    # before the table id. Resolve the authoritative IDs from table-list.
    authoritative = runner.lark(["base", "+table-list", "--base-token", str(base_token), "--limit", "100"])
    for item in extract_items(authoritative):
        if isinstance(item, Mapping) and item.get("name") in {name for name, _ in schemas}:
            resolved_id = item.get("table_id") or item.get("id")
            if resolved_id:
                table_ids[str(item["name"])] = str(resolved_id)
    return {"name": name, "base_token": str(base_token), "url": str(base_url), "tables": table_ids}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sync", description="Feishu Agent knowledge synchronization")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-config")
    validate.add_argument("--config", required=True)
    validate.add_argument("--strict", action="store_true")
    scan_cmd = sub.add_parser("scan")
    scan_cmd.add_argument("--config", required=True)
    scan_cmd.add_argument("--dry-run", action="store_true", help="preview without writing proposals, runs, or cursors")
    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("--config", required=True)
    apply_cmd.add_argument("--proposal-id", required=True)
    apply_cmd.add_argument("--dry-run", action="store_true")
    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("--config", required=True)
    bootstrap = sub.add_parser("bootstrap-base")
    bootstrap.add_argument("--name", default="Agent 知识同步管理")
    bootstrap.add_argument("--lark-cli-path")
    bootstrap.add_argument("--base-token", help="resume setup in an existing Base")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "bootstrap-base":
            config = {"runtime": {"lark_cli_path": args.lark_cli_path}} if args.lark_cli_path else {}
            result = bootstrap_base(ToolRunner(config), args.name, args.base_token or "")
        else:
            config, config_path = load_config(args.config)
            if args.command == "validate-config":
                warnings = validate_config(config, strict=args.strict)
                result = {"valid": True, "warnings": warnings, "agent_id": config["agent"]["id"]}
            else:
                runner = ToolRunner(config)
                store = make_store(config, config_path, runner)
                if args.command == "scan":
                    result = scan(config, config_path, runner, store, args.dry_run)
                elif args.command == "apply":
                    result = apply_proposal(config, args.proposal_id, runner, store, args.dry_run)
                elif args.command == "status":
                    result = store.status()
                else:
                    raise SyncError(f"未知命令: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SyncError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
