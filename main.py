from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.utils.session_waiter import SessionController, session_waiter
from .qq_profile_sync import QQProfileSync


@dataclass
class KeywordMapping:
    keyword: str
    persona_id: str
    case_sensitive: bool = False
    reply_template: str = ""

    def matches(self, text: str) -> bool:
        if not self.case_sensitive:
            text = text.lower()
            keyword = self.keyword.lower()
        else:
            keyword = self.keyword
        return keyword in text


@register(
    "persona_plus",
    "Railgun",
    "扩展人格管理能力并支持关键词自动切换",
    "1.0",
    "https://github.com/railgun19457/astrbot_plugin_persona_plus",
)
class PersonaPlus(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context: Context = context
        self.config: AstrBotConfig | None = config
        self.persona_mgr: PersonaManager = context.persona_manager
        self.keyword_mappings: list[KeywordMapping] = []
        self.auto_switch_scope: str = "conversation"
        self.keyword_switch_enabled: bool = True
        self.manage_wait_timeout: int = 120
        self.require_admin_for_manage: bool = False
        self.auto_switch_announce: bool = False
        self.clear_context_on_switch: bool = False
        self.qq_sync = QQProfileSync(context)
        self._load_config()

    def _load_config(self) -> None:
        if not self.config:
            self.qq_sync.load_config(None)
            logger.warning("Persona+ 未载入专用配置，将使用默认值。")
            return

        mappings_raw = self.config.get("keyword_mappings", "")
        loaded: list[KeywordMapping] = []

        if mappings_raw is None:
            entries: list[str] = []
        elif isinstance(mappings_raw, str):
            entries = mappings_raw.splitlines()
        else:
            logger.warning(
                "Persona+ 关键词配置应为文本，实际收到 %r (类型 %s)",
                mappings_raw,
                type(mappings_raw).__name__,
            )
            entries = str(mappings_raw).splitlines()

        for raw_entry in entries:
            entry = raw_entry.strip()
            if not entry or entry.startswith("#"):
                continue
            try:
                loaded.append(self._parse_mapping_entry(entry))
            except Exception as exc:  # noqa: BLE001
                logger.error("Persona+ 解析关键词配置失败: %s", exc)

        self.keyword_mappings = [m for m in loaded if m.keyword and m.persona_id]
        self.auto_switch_scope = self.config.get("auto_switch_scope", "conversation")
        self.keyword_switch_enabled = bool(
            self.config.get("enable_keyword_switching", True)
        )
        self.require_admin_for_manage = bool(
            self.config.get("require_admin_for_manage", False)
        )
        self.auto_switch_announce = bool(
            self.config.get("enable_auto_switch_announce", False)
        )
        self.clear_context_on_switch = bool(
            self.config.get("clear_context_on_switch", False)
        )
        raw_timeout = self.config.get("manage_wait_timeout_seconds", 120)
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            logger.warning(
                "Persona+ manage_wait_timeout_seconds=%r 非法，使用默认值 120",
                raw_timeout,
            )
            timeout = 120
        if timeout <= 0:
            logger.warning(
                "Persona+ manage_wait_timeout_seconds=%r 必须为正数，已重置为 120",
                raw_timeout,
            )
            timeout = 120
        self.manage_wait_timeout = timeout
        self.qq_sync.load_config(self.config)

        logger.info(
            "Persona+ 配置加载完成：关键词 %d 项，自动切换范围=%s，关键词自动切换=%s，QQ同步=%s",
            len(self.keyword_mappings),
            self.auto_switch_scope,
            self.keyword_switch_enabled,
            self.qq_sync.describe_settings(),
        )
        logger.info(
            "Persona+ 管理权限配置：require_admin_for_manage=%s",
            self.require_admin_for_manage,
        )
        logger.info(
            "Persona+ 管理操作等待超时：manage_wait_timeout=%ss",
            self.manage_wait_timeout,
        )
        logger.info(
            "Persona+ 自动切换提示：enable_auto_switch_announce=%s",
            self.auto_switch_announce,
        )
        logger.info(
            "Persona+ 切换后清空上下文：clear_context_on_switch=%s",
            self.clear_context_on_switch,
        )

    # ==================== 工具函数 ====================
    @staticmethod
    def _parse_mapping_entry(entry: str) -> KeywordMapping:
        left, sep, right = entry.partition(":")
        if sep == "":
            raise ValueError(f"无效的关键词映射格式：{entry!r}，应为 关键词:人格ID。")

        persona_id = right.strip()
        if not persona_id:
            raise ValueError(f"无效的人格 ID：{entry!r}。")

        keyword_part = left.strip()
        keyword = keyword_part
        if "|" in keyword_part:
            _, keyword_raw = keyword_part.split("|", 1)
            keyword = keyword_raw.strip()
            logger.warning(
                "Persona+ 不再支持匹配模式配置，已将 %r 按包含匹配处理。",
                entry,
            )

        if not keyword:
            raise ValueError(f"无效的关键词内容：{entry!r}。")

        return KeywordMapping(keyword=keyword, persona_id=persona_id)

    @staticmethod
    def _parse_persona_payload(raw_text: str) -> tuple[str, list[str]]:
        """从用户传入的文本中解析 system_prompt 与 begin_dialogs.

        支持两种格式：
        1. 纯文本 => 全部作为 system_prompt
        2. JSON => {"system_prompt": str, "begin_dialogs": ["user", "assistant", ...]}
        """

        raw_text = raw_text.strip()
        if not raw_text:
            raise ValueError("内容为空，无法导入人格。")

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return raw_text, []

        if not isinstance(payload, dict):
            raise ValueError("JSON 格式必须是对象，包含 system_prompt 字段。")

        prompt = payload.get("system_prompt")
        if not prompt:
            raise ValueError("JSON 中缺少 system_prompt 字段。")
        begin_dialogs = payload.get("begin_dialogs", [])
        if begin_dialogs and len(begin_dialogs) % 2 != 0:
            raise ValueError("begin_dialogs 条目数量必须为偶数（用户/助手交替）。")

        return str(prompt), [str(x) for x in begin_dialogs]

    @staticmethod
    async def _read_file_component(component: Comp.BaseMessageComponent) -> str | None:
        if isinstance(component, Comp.File):
            content_path = await component.get_file()
            if not content_path:
                return None
            path = Path(content_path)
            if path.suffix.lower() not in {".txt", ".md", ".json"}:
                raise ValueError("仅支持导入 txt / md / json 文件。")
            return path.read_text(encoding="utf-8")
        return None

    async def _extract_persona_from_event(self, event: AstrMessageEvent) -> str:
        """从消息链中提取文本或文件内容。"""

        if event.message_str.strip():
            return event.message_str

        for component in event.message_obj.message:
            data = await self._read_file_component(component)
            if data:
                return data
        raise ValueError("未检测到可解析的文本或文件内容。")

    async def _create_persona(
        self,
        persona_id: str,
        system_prompt: str,
        begin_dialogs: list[str],
        tools: list[str] | None = None,
    ) -> None:
        try:
            await self.persona_mgr.get_persona(persona_id)
        except ValueError:
            await self.persona_mgr.create_persona(
                persona_id=persona_id,
                system_prompt=system_prompt,
                begin_dialogs=begin_dialogs if begin_dialogs else None,
                tools=tools,
            )
            return

        raise ValueError(
            f"人格 {persona_id} 已存在，请使用 /persona_plus update {persona_id}。"
        )

    async def _switch_persona(
        self,
        event: AstrMessageEvent,
        persona_id: str,
        announce: str | None = None,
    ) -> MessageEventResult | None:
        """切换对话或配置中的默认人格。"""

        await self.persona_mgr.get_persona(persona_id)
        umo = event.unified_msg_origin
        scope = self.auto_switch_scope
        history_reset = [] if self.clear_context_on_switch else None

        if scope == "conversation":
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                await self.context.conversation_manager.new_conversation(
                    unified_msg_origin=umo,
                    persona_id=persona_id,
                )
            else:
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=umo,
                    conversation_id=cid,
                    persona_id=persona_id,
                    history=history_reset,
                )
        elif scope == "session":
            config = self.context.astrbot_config_mgr.get_conf(umo)
            if config:
                provider_settings = config.setdefault("provider_settings", {})
                provider_settings["default_personality"] = persona_id
                config.save_config()
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if cid:
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=umo,
                    conversation_id=cid,
                    persona_id=persona_id,
                    history=history_reset,
                )
        elif scope == "global":
            config = self.context.astrbot_config_mgr.default_conf
            provider_settings = config.setdefault("provider_settings", {})
            provider_settings["default_personality"] = persona_id
            config.save_config()
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if cid:
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=umo,
                    conversation_id=cid,
                    persona_id=persona_id,
                    history=history_reset,
                )

        await self.qq_sync.maybe_sync_profile(event, persona_id)

        if announce:
            return event.plain_result(announce)
        return None

    def _collect_admin_ids(self, umo: str | None) -> set[str]:
        admin_ids: set[str] = set()
        default_conf = self.context.get_config()
        admin_ids.update(str(admin) for admin in default_conf.get("admins_id", []))
        if umo:
            scoped_conf = self.context.astrbot_config_mgr.get_conf(umo)
            if scoped_conf:
                admin_ids.update(
                    str(admin) for admin in scoped_conf.get("admins_id", [])
                )
        return admin_ids

    def _has_manage_permission(self, event: AstrMessageEvent) -> bool:
        if event.is_admin():
            return True
        sender_id = str(event.get_sender_id())
        admin_ids = self._collect_admin_ids(event.unified_msg_origin)
        return sender_id in admin_ids

    def _has_permission(
        self,
        event: AstrMessageEvent,
        *,
        manage_operation: bool,
        force_admin: bool = False,
    ) -> bool:
        need_admin = force_admin or (manage_operation and self.require_admin_for_manage)
        if not need_admin:
            return True
        return self._has_manage_permission(event)

    # ==================== 指令：人格管理 ====================
    @filter.command_group("persona_plus", alias={"pp", "persona+"})
    def persona_plus(self):
        """Persona+ 插件命令入口。"""
        # 指令组不需要实现

    @persona_plus.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        """展示 Persona+ 指令列表。"""

        if not self._has_permission(event, manage_operation=False):
            yield event.plain_result("此操作需要管理员权限。")
            return

        sections = [
            "Persona+ 扩展指令（别名 /pp /persona+ 可用）：",
            "- /persona_plus help — 查看帮助与配置说明",
            "- /persona_plus list — 列出所有人格",
            "- /persona_plus view <persona_id> — 查看人格详情",
            "- /persona_plus create <persona_id> — 创建新人格，随后发送内容或文件 (支持 txt/md/json)",
            "- /persona_plus update <persona_id> — 更新人格，随后发送内容或文件 (支持 txt/md/json)",
            "- /persona_plus delete <persona_id> — 删除人格 (管理员)",
        ]
        yield event.plain_result("\n".join(sections))

    @persona_plus.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出所有已注册人格。"""

        if not self._has_permission(event, manage_operation=False):
            yield event.plain_result("此操作需要管理员权限。")
            return

        personas = await self.persona_mgr.get_all_personas()
        if not personas:
            yield event.plain_result("当前没有人格，请先在控制台或通过指令创建。")
            return

        lines = ["已载入人格："]
        for persona in personas:
            begin_cnt = len(persona.begin_dialogs or [])
            tool_cnt = len(persona.tools or []) if persona.tools is not None else "ALL"
            lines.append(
                f"- {persona.persona_id} | 预设对话: {begin_cnt} | 工具: {tool_cnt}"
            )
        yield event.plain_result("\n".join(lines))

    @persona_plus.command("view")
    async def cmd_view(self, event: AstrMessageEvent, persona_id: str):
        """查看指定人格详情。"""

        if not self._has_permission(event, manage_operation=False):
            yield event.plain_result("此操作需要管理员权限。")
            return

        try:
            persona = await self.persona_mgr.get_persona(persona_id)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        begin_dialogs = persona.begin_dialogs or []
        tools = persona.tools

        lines = [
            f"人格 {persona.persona_id}",
            "----------------",
            "System Prompt:",
            persona.system_prompt,
        ]

        if begin_dialogs:
            lines.append("\n预设对话：")
            for idx, dialog in enumerate(begin_dialogs, start=1):
                role = "用户" if idx % 2 == 1 else "助手"
                lines.append(f"[{role}] {dialog}")

        if tools is None:
            lines.append("\n工具：使用全部可用工具")
        elif len(tools) == 0:
            lines.append("\n工具：已禁用所有工具")
        else:
            lines.append("\n工具：" + ", ".join(tools))

        yield event.plain_result("\n".join(lines))

    @persona_plus.command("delete")
    async def cmd_delete(self, event: AstrMessageEvent, persona_id: str):
        """删除指定人格（仅管理员）。"""

        if not self._has_permission(event, manage_operation=True, force_admin=True):
            yield event.plain_result("此操作需要管理员权限。")
            return

        try:
            await self.persona_mgr.delete_persona(persona_id)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        yield event.plain_result(f"人格 {persona_id} 已删除。")

    @persona_plus.command("create")
    async def cmd_create(self, event: AstrMessageEvent, persona_id: str):
        """从文本或文件创建新人格。"""

        if not self._has_permission(event, manage_operation=True):
            yield event.plain_result("此操作需要管理员权限。")
            return

        try:
            await self.persona_mgr.get_persona(persona_id)
        except ValueError:
            pass
        else:
            yield event.plain_result(
                f"人格 {persona_id} 已存在，请使用 /persona_plus update {persona_id}。"
            )
            return

        yield event.plain_result(
            "请发送人格内容或文件 (txt/md/json)，将在收到后立即处理。"
        )

        @session_waiter(timeout=self.manage_wait_timeout)
        async def create_waiter(
            controller: SessionController, next_event: AstrMessageEvent
        ) -> None:
            try:
                raw_text = await self._extract_persona_from_event(next_event)
                system_prompt, begin_dialogs = self._parse_persona_payload(raw_text)
                await self._create_persona(
                    persona_id=persona_id,
                    system_prompt=system_prompt,
                    begin_dialogs=begin_dialogs,
                )
            except ValueError as exc:
                await next_event.send(next_event.plain_result(f"创建失败：{exc}"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("创建人格时出现异常")
                await next_event.send(next_event.plain_result(f"创建失败：{exc}"))
            else:
                await next_event.send(
                    next_event.plain_result(f"人格 {persona_id} 创建成功。")
                )
            finally:
                controller.stop()

        async def wait_for_create() -> None:
            try:
                await create_waiter(event)
            except TimeoutError:
                await event.send(event.plain_result("等待人格内容超时，操作已取消。"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("创建人格等待流程异常")
                await event.send(event.plain_result(f"创建流程异常：{exc}"))

        asyncio.create_task(wait_for_create())
        event.stop_event()
        return

    @persona_plus.command("update")
    async def cmd_update(self, event: AstrMessageEvent, persona_id: str):
        """更新现有人格，使用下一条消息提供内容。"""

        if not self._has_permission(event, manage_operation=True):
            yield event.plain_result("此操作需要管理员权限。")
            return

        try:
            await self.persona_mgr.get_persona(persona_id)
        except ValueError:
            yield event.plain_result(f"未找到人格 {persona_id}，请先创建该人格。")
            return

        yield event.plain_result(
            "请发送新的人格内容或文件 (txt/md/json)，将在收到后立即更新。"
        )

        @session_waiter(timeout=self.manage_wait_timeout)
        async def update_waiter(
            controller: SessionController, next_event: AstrMessageEvent
        ) -> None:
            try:
                raw_text = await self._extract_persona_from_event(next_event)
                system_prompt, begin_dialogs = self._parse_persona_payload(raw_text)
                await self.persona_mgr.update_persona(
                    persona_id=persona_id,
                    system_prompt=system_prompt,
                    begin_dialogs=begin_dialogs if begin_dialogs else None,
                )
            except ValueError as exc:
                await next_event.send(next_event.plain_result(f"更新失败：{exc}"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("更新人格时出现异常")
                await next_event.send(next_event.plain_result(f"更新失败：{exc}"))
            else:
                await next_event.send(
                    next_event.plain_result(f"人格 {persona_id} 更新成功。")
                )
            finally:
                controller.stop()

        async def wait_for_update() -> None:
            try:
                await update_waiter(event)
            except TimeoutError:
                await event.send(event.plain_result("等待人格内容超时，操作已取消。"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("更新人格等待流程异常")
                await event.send(event.plain_result(f"更新流程异常：{exc}"))

        asyncio.create_task(wait_for_update())
        event.stop_event()
        return

    # ==================== 自动切换监听 ====================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = event.get_message_str()
        if not text or not self.keyword_switch_enabled or not self.keyword_mappings:
            return

        for mapping in self.keyword_mappings:
            if mapping.matches(text):
                announce = None
                if mapping.reply_template:
                    announce = mapping.reply_template.format(
                        persona_id=mapping.persona_id
                    )
                elif self.auto_switch_announce:
                    announce = f"已切换人格为 {mapping.persona_id}"
                result = await self._switch_persona(
                    event,
                    persona_id=mapping.persona_id,
                    announce=announce,
                )
                if result is not None:
                    yield result
                break

    async def terminate(self):
        """插件卸载时的清理逻辑。"""

        logger.info("Persona+ 插件卸载，已清理状态。")
