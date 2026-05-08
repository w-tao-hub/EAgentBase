"""自定义子代理 md 配置加载器。

扫描项目根目录 agents/*.md 并解析 frontmatter，
生成 CustomSubAgentDefinition 列表供 profile builder 使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# 允许出现在 md frontmatter 中的字段集合
_ALLOWED_FIELDS = {"name", "description", "tools", "skills", "max_turns", "hook_profile"}

# 禁止出现的字段集合，这些字段属于 Agent 模型层敏感配置，
# 不应通过 md 文件暴露，避免安全风险和配置混乱
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

    name: str  # 子代理标识名称，与默认定义重名时会报错
    description: str  # 子代理功能描述
    prompt: str  # md 正文内容，会作为子代理的 system_prompt
    tools: tuple[str, ...] | None = None  # 按名称引用的工具列表，None 表示不加载
    skills: tuple[str, ...] | None = None  # 按名称引用的 skill 列表，None 表示不加载
    max_turns: int | None = None  # 最大对话轮数，None 使用全局默认值
    hook_profile: str | None = None  # 引用的 Hook profile 名称
    source_path: Path | None = None  # 源文件路径，便于调试和审计


class CustomSubAgentLoader:
    """扫描项目根目录 agents/*.md 并解析自定义子代理配置。

    目录不存在时按空目录处理，返回空列表。
    遇到默认子代理重名、非法字段或格式错误时直接抛出 ValueError。
    """

    def __init__(self, agents_dir: Path, reserved_names: set[str] | None = None) -> None:
        """保存自定义子代理目录路径和保留名称集合。

        Args:
            agents_dir: agents 目录路径，不存在时 load() 返回空列表
            reserved_names: 不可复用的名称集合（如默认子代理名称）
        """
        self._agents_dir = agents_dir  # 保存 agents 目录路径
        self._reserved_names = reserved_names or set()  # 保留名称集合，用于冲突检测

    def load(self) -> list[CustomSubAgentDefinition]:
        """加载目录下的全部自定义子代理配置。

        Returns:
            按文件名排序的 CustomSubAgentDefinition 列表

        Raises:
            ValueError: 目录存在但不是目录、配置包含非法字段、或名称冲突
        """
        if not self._agents_dir.exists():  # 目录不存在时返回空列表，不阻塞启动
            return []
        if not self._agents_dir.is_dir():  # 路径存在但不是目录时直接失败
            raise ValueError(f"agents 路径不是目录: {self._agents_dir}")

        definitions: list[CustomSubAgentDefinition] = []  # 累积解析成功的定义
        seen_names: set[str] = set()  # 记录已处理的子代理名称，用于去重
        for path in sorted(self._agents_dir.glob("*.md")):  # 按文件名排序遍历所有 md 文件
            definition = self._load_one(path)  # 解析单个 md 文件
            if definition.name in self._reserved_names:  # 与默认子代理重名时报错
                raise ValueError(f"默认子代理重名: {definition.name}")
            if definition.name in seen_names:  # 同一目录下出现重复名称时报错
                raise ValueError(f"子代理名称重复: {definition.name}")
            seen_names.add(definition.name)  # 记录已见名称
            definitions.append(definition)  # 添加到结果列表
        return definitions

    def _load_one(self, path: Path) -> CustomSubAgentDefinition:
        """解析单个 md 子代理配置文件。

        Args:
            path: md 文件路径

        Returns:
            解析完成的 CustomSubAgentDefinition 实例

        Raises:
            ValueError: frontmatter 格式错误、字段不合法、或必填字段缺失
        """
        frontmatter, body = self._split_frontmatter(path.read_text(encoding="utf-8"), path)  # 拆分 frontmatter 和正文

        # 检查未知字段和禁止字段，同时检查比先后检查更有利于错误信息完整
        unknown_fields = set(frontmatter) - _ALLOWED_FIELDS  # 不在允许字段集合中的字段
        banned_fields = set(frontmatter) & _BANNED_FIELDS  # 与禁止字段集合的交集
        if banned_fields:  # 出现禁止字段时直接报错
            raise ValueError(f"子代理配置包含禁用字段: {path}: {sorted(banned_fields)}")
        if unknown_fields:  # 出现未知字段时直接报错
            raise ValueError(f"子代理配置包含未知字段: {path}: {sorted(unknown_fields)}")

        name = str(frontmatter.get("name", "")).strip()  # 子代理名称，必填
        description = str(frontmatter.get("description", "")).strip()  # 子代理描述，必填
        prompt = body.strip()  # 正文内容，不能为空
        if not name:  # 名称缺失时报错
            raise ValueError(f"子代理缺少 name: {path}")
        if not description:  # 描述缺失时报错
            raise ValueError(f"子代理缺少 description: {path}")
        if not prompt:  # 正文为空时报错
            raise ValueError(f"子代理 prompt 正文为空: {path}")

        max_turns = self._parse_positive_int(frontmatter.get("max_turns"), path)  # 解析 max_turns 为正整数
        return CustomSubAgentDefinition(
            name=name,
            description=description,
            prompt=prompt,
            tools=self._parse_optional_string_list(frontmatter.get("tools")),  # 解析工具列表
            skills=self._parse_optional_string_list(frontmatter.get("skills")),  # 解析 skill 列表
            max_turns=max_turns,
            hook_profile=self._parse_optional_string(frontmatter.get("hook_profile")),  # 解析 hook profile 名称
            source_path=path,  # 记录源文件路径
        )

    @staticmethod
    def _split_frontmatter(raw: str, path: Path) -> tuple[dict[str, object], str]:
        """解析最小 YAML 子集，支持 key: value 和缩进列表格式。

        不使用 pyyaml 依赖，仅支持以下语法：
        - key: value（简单键值对）
        - key:（空值 → 开启列表模式）
        -   - item（缩进列表项）

        Args:
            raw: md 文件原始文本
            path: 文件路径，仅用于错误消息

        Returns:
            (frontmatter 字典, 正文文本) 的元组

        Raises:
            ValueError: frontmatter 起始标记缺失、未闭合、或格式非法
        """
        if not raw.startswith("---\n"):  # 要求必须以 --- 行开头
            raise ValueError(f"子代理缺少 frontmatter: {path}")
        lines = raw.splitlines()  # 按行拆分原始文本
        end_index = None  # 记录第二个 --- 分隔符的索引
        for index in range(1, len(lines)):  # 从第一行之后开始查找结束标记
            if lines[index].strip() == "---":  # 找到结束分隔符
                end_index = index
                break
        if end_index is None:  # 未找到结束标记时报错
            raise ValueError(f"子代理 frontmatter 未闭合: {path}")

        data: dict[str, object] = {}  # frontmatter 解析结果
        current_list_key: str | None = None  # 当前正在构建的列表键名
        for line in lines[1:end_index]:  # 遍历 frontmatter 区域内的每一行
            stripped = line.strip()  # 去除首尾空白
            if not stripped:  # 跳过空行
                continue
            if stripped.startswith("- "):  # 列表项行
                if current_list_key is None:  # 列表项没有对应的键时报错
                    raise ValueError(f"子代理 frontmatter 列表缺少键: {path}: {line}")
                data.setdefault(current_list_key, [])  # 确保键对应的值是列表
                assert isinstance(data[current_list_key], list)  # 类型断言确保安全
                data[current_list_key].append(stripped.removeprefix("- ").strip())  # 追加列表项
                continue
            if ":" not in line:  # 既不是列表项也不是键值对时报错
                raise ValueError(f"子代理 frontmatter 非法行: {path}: {line}")
            key, value = line.split(":", 1)  # 以第一个冒号分割键和值
            key = key.strip()  # 清理键名空白
            value = value.strip().strip('"').strip("'")  # 清理值两端的空白和引号
            if value:  # 如果值非空，作为简单键值对处理
                data[key] = value  # 存储简单键值
                current_list_key = None  # 退出列表模式
            else:  # 如果值为空，进入列表模式
                data[key] = []  # 初始化为空列表
                current_list_key = key  # 记录当前列表键

        body = "\n".join(lines[end_index + 1:])  # 提取正文部分
        return data, body

    @staticmethod
    def _parse_optional_string_list(value: object) -> tuple[str, ...] | None:
        """把 frontmatter 中的字符串或列表字段归一为字符串元组。

        支持三种输入形态：
        - None：返回 None 表示未配置
        - 逗号分隔字符串：'Read, Bash' → ('Read', 'Bash')
        - 列表：['Read', 'Bash'] → ('Read', 'Bash')

        Args:
            value: 从 frontmatter 解析出的原始值

        Returns:
            归一化后的字符串元组，None 表示未配置
        """
        if value is None:  # 未配置时返回 None
            return None
        if isinstance(value, str):  # 逗号分隔字符串：按逗号切分并去空白
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, list):  # 列表格式：逐元素转字符串并去空白
            return tuple(str(item).strip() for item in value if str(item).strip())
        raise ValueError(f"字段必须是字符串或字符串列表: {value!r}")  # 非法类型时报错

    @staticmethod
    def _parse_optional_string(value: object) -> str | None:
        """把可选字符串字段归一化。

        None 或空字符串均返回 None。

        Args:
            value: 从 frontmatter 解析出的原始值

        Returns:
            归一化后的字符串，空字符串视为 None
        """
        if value is None:  # 未配置时返回 None
            return None
        parsed = str(value).strip()  # 去空白
        return parsed or None  # 空字符串返回 None

    @staticmethod
    def _parse_positive_int(value: object, path: Path) -> int | None:
        """解析正整数 max_turns。

        Args:
            value: 从 frontmatter 解析出的原始值
            path: 源文件路径，用于错误消息

        Returns:
            解析后的正整数，None 或空值返回 None

        Raises:
            ValueError: 值不是正整数
        """
        if value is None or value == "":  # 未配置或空值返回 None
            return None
        try:
            parsed = int(str(value))  # 尝试转为整数
        except ValueError as exc:  # 转换失败时报错
            raise ValueError(f"max_turns 必须是正整数: {path}") from exc
        if parsed <= 0:  # 非正数时报错
            raise ValueError(f"max_turns 必须是正整数: {path}")
        return parsed
