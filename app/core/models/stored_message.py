"""按模型协议直接存储的消息模型定义。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

from datetime import datetime  # 导入日期时间类，承载消息创建时间
from typing import Any  # 导入任意类型，兼容工具调用等协议字段
import uuid  # 导入 UUID 模块，用于生成消息唯一标识

from pydantic import BaseModel, ConfigDict, Field  # 导入 Pydantic 模型与字段工具


class StoredMessageMeta(BaseModel):
    """表示直接随模型协议消息一起落库的附加元数据。"""

    # 消息唯一标识，用于摘要定位、去重和跨阶段追踪
    message_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="消息唯一标识(UUID)"
    )

    # 消息写入存储时的创建时间
    created_at: datetime = Field(description="消息创建时间")

    # 标记该消息是否只给模型看、对用户隐藏
    is_meta: bool = Field(
        default=False,
        description="是否为模型可见但对用户隐藏的元消息"
    )

    # 记录该消息来源于哪一次 Run，便于后续追踪执行链路
    source_run_id: str | None = Field(
        default=None,
        description="产生该消息的 Run ID"
    )

    # 若该消息归属于某个 child 上下文，则记录会话内稳定 child_id
    child_id: str | None = Field(
        default=None,
        description="消息关联的会话内稳定 child_id"
    )

    # 消息所属的子代理类型，用于 child resume 一致性校验
    subagent_type: str | None = Field(
        default=None,
        description="消息所属的子代理类型，用于 child resume 一致性校验",
    )

    def to_storage_dict(self) -> dict[str, Any]:
        """将元数据序列化为可直接落库的字典。"""
        # 这里显式使用 datetime.isoformat()，与现有 Session/Run 存储格式保持一致，
        # 避免 Pydantic JSON 模式把 UTC 序列化成 `Z` 造成断言和旧数据风格不统一。
        data: dict[str, Any] = {
            "message_id": self.message_id,
            "created_at": self.created_at.isoformat(),
            "is_meta": self.is_meta,
        }
        if self.source_run_id is not None:
            data["source_run_id"] = self.source_run_id
        if self.child_id is not None:
            data["child_id"] = self.child_id
        if self.subagent_type is not None:
            data["subagent_type"] = self.subagent_type
        return data

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "StoredMessageMeta":
        """从存储字典恢复元数据模型。"""
        return cls.model_validate(data)


class StoredMessage(BaseModel):
    """表示按模型协议字段直接存储的一条消息。"""

    # 允许调用方使用 Python 字段名 `meta` 构造，同时序列化时输出 `_meta`
    model_config = ConfigDict(populate_by_name=True)

    # 模型协议中的消息角色，例如 user、assistant、system、tool
    role: str = Field(min_length=1, description="模型协议角色")

    # 模型协议中的消息正文；兼容纯文本和结构化 content 数组
    content: str | list[dict[str, Any]] | None = Field(
        default=None,
        description="模型协议消息正文"
    )

    # assistant 发起工具调用时的原生协议字段
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None,
        description="助手消息携带的工具调用协议数据"
    )

    # DeepSeek thinking 模型返回的思考内容。
    # 该字段只对 assistant 消息生效，用于后续继续推理时原样回传给模型。
    reasoning_content: str | None = Field(
        default=None,
        description="助手消息携带的 reasoning_content"
    )

    # tool 消息关联的工具调用 ID
    tool_call_id: str | None = Field(
        default=None,
        description="工具消息关联的 tool_call_id"
    )

    # tool 消息中的工具名称
    name: str | None = Field(
        default=None,
        description="工具消息中的工具名称"
    )

    # 存储层最小附加元数据，序列化时使用 `_meta` 键以贴合最终存储格式
    meta: StoredMessageMeta = Field(
        alias="_meta",
        serialization_alias="_meta",
        validation_alias="_meta",
        description="消息的存储层附加元数据"
    )

    def to_storage_dict(self) -> dict[str, Any]:
        """将消息序列化为可直接落库的模型协议字典。"""
        data: dict[str, Any] = {
            "role": self.role,
            "_meta": self.meta.to_storage_dict(),
        }
        if self.content is not None:
            data["content"] = self.content
        if self.tool_calls is not None:
            data["tool_calls"] = self.tool_calls
        if self.reasoning_content is not None:
            data["reasoning_content"] = self.reasoning_content
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            data["name"] = self.name
        return data

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "StoredMessage":
        """从存储字典恢复 StoredMessage 实例。"""
        return cls.model_validate(data)

    @classmethod
    def create(
        cls,
        *,
        role: str,
        timestamp: datetime,
        content: str | list[dict[str, Any]] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        message_id: str | None = None,
        is_meta: bool = False,
        source_run_id: str | None = None,
        child_id: str | None = None,
        subagent_type: str | None = None,
    ) -> "StoredMessage":
        """构造一条带完整 `_meta` 的 StoredMessage。"""
        return cls(
            role=role,
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            tool_call_id=tool_call_id,
            name=name,
            meta=StoredMessageMeta(
                message_id=message_id or uuid.uuid4().hex,
                created_at=timestamp,
                is_meta=is_meta,
                source_run_id=source_run_id,
                child_id=child_id,
                subagent_type=subagent_type,
            ),
        )

    def with_meta_overrides(
        self,
        *,
        source_run_id: str | None = None,
        child_id: str | None = None,
        subagent_type: str | None = None,
    ) -> "StoredMessage":
        """返回一条仅覆盖指定 `_meta` 字段的新消息。"""
        next_source_run_id = self.meta.source_run_id if source_run_id is None else source_run_id
        next_child_id = self.meta.child_id if child_id is None else child_id
        next_subagent_type = self.meta.subagent_type if subagent_type is None else subagent_type
        return self.model_copy(
            update={
                "meta": self.meta.model_copy(
                    update={
                        "source_run_id": next_source_run_id,
                        "child_id": next_child_id,
                        "subagent_type": next_subagent_type,
                    }
                )
            },
            deep=True,
        )

    @property
    def message_id(self) -> str:
        """兼容运行时按消息主字段读取 message_id。"""
        return self.meta.message_id

    @property
    def timestamp(self) -> datetime:
        """兼容运行时按消息主字段读取时间戳。"""
        return self.meta.created_at

    @property
    def is_meta(self) -> bool:
        """兼容运行时按消息主字段读取隐藏标记。"""
        return self.meta.is_meta
