"""自定义子代理 md 配置加载器。

扫描项目根目录 agents/*.md 并解析 frontmatter，
生成 CustomSubAgentDefinition 列表供 profile builder 使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# md frontmatter 允许/禁止字段
_ALLOWED_FIELDS = {"name", "description", "tools", "skills", "max_turns", "tool_hook_profiles", "model_hook_profiles", "mount_master_agents"}

# 禁止 Agent 模型层敏感配置通过 md 暴露
_BANNED_FIELDS = {
    "agent_id",
    "display_name",
    "model",
    "api_key",
    "key",
    "temperature",
    "reasoning_effort",
    "color",
    "guard_profile",
    "metadata",
}


@dataclass(frozen=True, slots=True)
class CustomSubAgentDefinition:
    """从 md frontmatter 解析出的自定义子代理配置。

    保持不可变语义，供 profile builder 后续消费。
    """

    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None
    max_turns: int | None = None
    tool_hook_profiles: tuple[str, ...] | None = None
    model_hook_profiles: tuple[str, ...] | None = None
    mount_master_agents: tuple[str, ...] | None = None
    source_path: Path | None = None


class CustomSubAgentLoader:
    """扫描 agents/*.md 并解析自定义子代理配置。"""

    def __init__(self, agents_dir: Path, reserved_names: set[str] | None = None) -> None:
        self._agents_dir = agents_dir
        self._reserved_names = reserved_names or set()

    def load(self) -> list[CustomSubAgentDefinition]:
        """加载目录下的全部自定义子代理配置。"""
        # 目录不存在时返回空列表，不阻塞启动
        if not self._agents_dir.exists():
            return []
        if not self._agents_dir.is_dir():
            raise ValueError(f"agents 路径不是目录: {self._agents_dir}")

        definitions: list[CustomSubAgentDefinition] = []
        seen_names: set[str] = set()
        for path in sorted(self._agents_dir.glob("*.md")):
            definition = self._load_one(path)
            if definition.name in self._reserved_names:
                raise ValueError(f"默认子代理重名: {definition.name}")
            if definition.name in seen_names:
                raise ValueError(f"子代理名称重复: {definition.name}")
            seen_names.add(definition.name)
            definitions.append(definition)
        return definitions

    def _load_one(self, path: Path) -> CustomSubAgentDefinition:
        """解析单个 md 子代理配置文件。"""
        frontmatter, body = self._split_frontmatter(path.read_text(encoding="utf-8"), path)

        # 同时检查未知和禁止字段，错误信息更完整
        unknown_fields = set(frontmatter) - _ALLOWED_FIELDS
        banned_fields = set(frontmatter) & _BANNED_FIELDS
        if banned_fields:
            raise ValueError(f"子代理配置包含禁用字段: {path}: {sorted(banned_fields)}")
        if unknown_fields:
            raise ValueError(f"子代理配置包含未知字段: {path}: {sorted(unknown_fields)}")

        name = str(frontmatter.get("name", "")).strip()
        description = str(frontmatter.get("description", "")).strip()
        prompt = body.strip()
        if not name:
            raise ValueError(f"子代理缺少 name: {path}")
        if not description:
            raise ValueError(f"子代理缺少 description: {path}")
        if not prompt:
            raise ValueError(f"子代理 prompt 正文为空: {path}")

        max_turns = self._parse_positive_int(frontmatter.get("max_turns"), path)
        return CustomSubAgentDefinition(
            name=name,
            description=description,
            prompt=prompt,
            tools=self._parse_optional_string_list(frontmatter.get("tools")),
            skills=self._parse_optional_string_list(frontmatter.get("skills")),
            max_turns=max_turns,
            tool_hook_profiles=self._parse_optional_string_list(frontmatter.get("tool_hook_profiles")),
            model_hook_profiles=self._parse_optional_string_list(frontmatter.get("model_hook_profiles")),
            mount_master_agents=self._parse_optional_string_list(frontmatter.get("mount_master_agents")),
            source_path=path,
        )

    @staticmethod
    def _split_frontmatter(raw: str, path: Path) -> tuple[dict[str, object], str]:
        """解析最小 YAML 子集（key: value 和缩进列表），避免引入 pyyaml 依赖。"""
        if not raw.startswith("---\n"):
            raise ValueError(f"子代理缺少 frontmatter: {path}")
        lines = raw.splitlines()
        end_index = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                end_index = index
                break
        if end_index is None:
            raise ValueError(f"子代理 frontmatter 未闭合: {path}")

        data: dict[str, object] = {}
        current_list_key: str | None = None
        for line in lines[1:end_index]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("- "):
                if current_list_key is None:
                    raise ValueError(f"子代理 frontmatter 列表缺少键: {path}: {line}")
                data.setdefault(current_list_key, [])
                assert isinstance(data[current_list_key], list)
                data[current_list_key].append(stripped.removeprefix("- ").strip())
                continue
            if ":" not in line:
                raise ValueError(f"子代理 frontmatter 非法行: {path}: {line}")
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if value:
                data[key] = value
                current_list_key = None
            else:
                data[key] = []
                current_list_key = key

        body = "\n".join(lines[end_index + 1:])
        return data, body

    @staticmethod
    def _parse_optional_string_list(value: object) -> tuple[str, ...] | None:
        """将 frontmatter 字符串/列表字段归一为字符串元组。"""
        if value is None:
            return None
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, list):
            return tuple(str(item).strip() for item in value if str(item).strip())
        raise ValueError(f"字段必须是字符串或字符串列表: {value!r}")

    @staticmethod
    def _parse_optional_string(value: object) -> str | None:
        """可选字符串字段归一化，空字符串返回 None。"""
        if value is None:
            return None
        parsed = str(value).strip()
        return parsed or None

    @staticmethod
    def _parse_positive_int(value: object, path: Path) -> int | None:
        """解析正整数 max_turns。"""
        if value is None or value == "":
            return None
        try:
            parsed = int(str(value))
        except ValueError as exc:
            raise ValueError(f"max_turns 必须是正整数: {path}") from exc
        if parsed <= 0:
            raise ValueError(f"max_turns 必须是正整数: {path}")
        return parsed
