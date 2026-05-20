import json
import re
from pathlib import Path
from typing import Dict, Any, Optional, List, Set, Tuple
from datetime import datetime

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Plain, At

# 从消息文本中识别 QQ 号（QQ123、qq:123、纯数字等）
QQ_IN_TEXT_PATTERN = re.compile(
    r"(?:QQ|qq)[:\s]*(\d{5,12})|@(\d{5,12})\b|(?:^|[\s,，])(\d{8,12})(?:[\s,，]|$)"
)
FIELD_VALUE_PATTERN = re.compile(
    r"([\w\u4e00-\u9fff/]+)\s*=\s*([^,，]*(?:[,，](?!\s*[\w\u4e00-\u9fff/]+=)[^,，]*)*)"
)


class SoulMapManager:
    """
    用户画像管理系统 (SoulMap)
    - 所有字段统一为字符串类型，AI负责数据格式管理
    - 备注字段特殊处理：追加模式，保留最近N条
    """

    def __init__(
        self,
        data_path: Path,
        allowed_fields: list,
        max_notes_count: int = 5,
        max_note_length: int = 50,
    ):
        self.data_path = data_path
        self.allowed_fields = allowed_fields
        self.max_notes_count = max_notes_count
        self.max_note_length = max_note_length
        self._init_path()
        self.user_data = self._load_data("user_profiles.json")

    def _init_path(self):
        self.data_path.mkdir(parents=True, exist_ok=True)

    def _load_data(self, filename: str) -> Dict[str, Any]:
        path = self.data_path / filename
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"[SoulMap] 加载数据失败: {e}")
            return {}
        except (IOError, OSError) as e:
            logger.error(f"[SoulMap] 读取文件失败: {e}")
            return {}

    def _save_data(self):
        path = self.data_path / "user_profiles.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.user_data, f, ensure_ascii=False, indent=2)
        except (IOError, OSError) as e:
            logger.error(f"[SoulMap] 写入文件失败: {e}")
        except (TypeError, ValueError) as e:
            logger.error(f"[SoulMap] 序列化数据失败: {e}")

    def _get_user_key(self, user_id: str, session_id: Optional[str] = None) -> str:
        return f"{session_id}_{user_id}" if session_id else user_id

    def get_user_profile(self, user_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        key = self._get_user_key(user_id, session_id)
        return self.user_data.get(key, {}).copy()

    def _format_note_entry(self, content: str, contributor: Optional[str] = None) -> str:
        content = content.strip()[: self.max_note_length]
        if contributor:
            prefix = f"（{contributor}代记）"
            remain = max(0, self.max_note_length - len(prefix))
            return prefix + content[:remain]
        return content

    def update_field(
        self,
        user_id: str,
        field: str,
        value: str,
        session_id: Optional[str] = None,
        save: bool = True,
        contributor: Optional[str] = None,
    ) -> tuple:
        """更新字段值。contributor 非空时，备注会标注代录人。"""
        if field not in self.allowed_fields:
            return False, f"字段 '{field}' 不在允许列表中"

        key = self._get_user_key(user_id, session_id)
        if key not in self.user_data:
            self.user_data[key] = {}

        value = value.strip()

        if field == "备注":
            existing = self.user_data[key].get("备注", "")
            if existing:
                notes = [n.strip() for n in re.split(r"[；;]", existing) if n.strip()]
            else:
                notes = []
            raw_notes = [n.strip() for n in re.split(r"[；;]", value) if n.strip()]
            new_notes = [
                self._format_note_entry(n, contributor) for n in raw_notes
            ]
            for note in new_notes:
                if note and note not in notes:
                    notes.append(note)
            notes = notes[-self.max_notes_count :]
            value = "；".join(notes)

        self.user_data[key][field] = value
        self.user_data[key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if save:
            self._save_data()
        return True, f"已更新 {field}"

    def delete_field(self, user_id: str, field: str, session_id: Optional[str] = None, save: bool = True) -> tuple:
        """删除字段或备注条目（支持数字索引）。save=False 时跳过写盘（用于批量操作）"""
        key = self._get_user_key(user_id, session_id)
        if key not in self.user_data:
            return False, "没有找到你的画像数据"

        if field in self.user_data[key]:
            del self.user_data[key][field]
            self.user_data[key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if save:
                self._save_data()
            return True, f"已删除字段 {field}"

        if "备注" in self.user_data[key] and field.isdigit():
            idx = int(field) - 1
            notes = [n.strip() for n in re.split(r"[；;]", self.user_data[key]["备注"]) if n.strip()]
            if 0 <= idx < len(notes):
                deleted_note = notes.pop(idx)
                if notes:
                    self.user_data[key]["备注"] = "；".join(notes)
                else:
                    del self.user_data[key]["备注"]
                self.user_data[key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if save:
                    self._save_data()
                return True, f"已删除备注第{field}条：{deleted_note}"
            return False, f"备注第{field}条不存在"

        if "备注" in self.user_data[key]:
            notes = [n.strip() for n in re.split(r"[；;]", self.user_data[key]["备注"]) if n.strip()]
            new_notes = [n for n in notes if field not in n]
            if len(new_notes) < len(notes):
                if new_notes:
                    self.user_data[key]["备注"] = "；".join(new_notes)
                else:
                    del self.user_data[key]["备注"]
                self.user_data[key]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if save:
                    self._save_data()
                return True, f"已从备注中删除包含 '{field}' 的条目"

        return False, f"未找到字段或备注条目 '{field}'"

    def clear_profile(self, user_id: str, session_id: Optional[str] = None) -> bool:
        key = self._get_user_key(user_id, session_id)
        if key in self.user_data:
            del self.user_data[key]
            self._save_data()
            return True
        return False

    def format_profile_summary(self, user_id: str, session_id: Optional[str] = None) -> str:
        """格式化用户画像摘要"""
        profile = self.get_user_profile(user_id, session_id)
        if not profile:
            return "暂无记录"

        lines = []
        for field in self.allowed_fields:
            if field in profile and profile[field]:
                if field == "备注":
                    notes = [n.strip() for n in re.split(r"[；;]", profile[field]) if n.strip()]
                    notes_display = " ".join([f"{i}.{note}" for i, note in enumerate(notes, 1)])
                    lines.append(f"- 备注：{notes_display}")
                else:
                    lines.append(f"- {field}：{profile[field]}")

        return "\n".join(lines) if lines else "暂无记录"

    def format_related_profiles(
        self,
        user_ids: List[str],
        session_id: Optional[str] = None,
        exclude_user_id: Optional[str] = None,
    ) -> str:
        """格式化对话中相关用户（@、QQ号）的画像摘要"""
        lines = []
        seen: Set[str] = set()
        for uid in user_ids:
            uid = str(uid).strip()
            if not uid or uid in seen:
                continue
            if exclude_user_id and uid == str(exclude_user_id):
                continue
            seen.add(uid)
            summary = self.format_profile_summary(uid, session_id)
            if summary == "暂无记录":
                lines.append(f"【用户 {uid}】暂无画像记录")
            else:
                lines.append(f"【用户 {uid}】\n{summary}")
        return "\n\n".join(lines) if lines else "（本条消息未涉及其他已知用户）"

    def export_all_profiles(self) -> Dict[str, Any]:
        return self.user_data.copy()


@register("SoulMap", "柯尔", "AI驱动的用户画像收集系统，简洁设计，AI负责数据管理", "1.2.0")
class SoulMapPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        data_dir = StarTools.get_data_dir()

        allowed_fields = self.config.get(
            "allowed_fields",
            [
                "对用户的称呼",
                "性别",
                "年龄",
                "所在地",
                "兽设",
                "生日",
                "爱吃",
                "忌口",
                "爱好",
                "职业",
                "重要节日",
                "恐惧/弱点",
                "作息规律",
                "技能水平",
                "健康状况",
                "宠物",
                "备注",
            ],
        )
        max_notes_count = self.config.get("max_notes_count", 5)
        max_note_length = self.config.get("max_note_length", 50)

        self.manager = SoulMapManager(
            data_dir, allowed_fields, max_notes_count, max_note_length
        )

        self.profile_pattern = re.compile(r"\[Profile:\s*([^\]]+)\]", re.IGNORECASE)
        self.profile_for_pattern = re.compile(
            r"\[ProfileFor:\s*([^,\]]+)\s*,\s*([^\]]+)\]", re.IGNORECASE
        )
        self.delete_pattern = re.compile(r"\[ProfileDelete:\s*([^\]]+)\]", re.IGNORECASE)
        self.block_pattern = re.compile(
            r"\s*\[(?:Profile(?:For)?|ProfileDelete):[^\]]*\]\s*", re.IGNORECASE
        )

    @property
    def session_based(self) -> bool:
        return bool(self.config.get("session_based", False))

    def _get_session_id(self, event: AstrMessageEvent) -> Optional[str]:
        return event.unified_msg_origin if self.session_based else None

    def _get_allowed_fields_display(self) -> str:
        return "/".join(self.manager.allowed_fields)

    def _get_sender_nickname(self, event: AstrMessageEvent) -> str:
        try:
            sender = event.message_obj.sender
            if sender and getattr(sender, "nickname", None):
                return str(sender.nickname)
        except Exception:
            pass
        return ""

    def _get_contributor_label(self, event: AstrMessageEvent) -> str:
        nickname = self._get_sender_nickname(event)
        sender_id = event.get_sender_id()
        if nickname:
            return f"{nickname}/{sender_id}"
        return str(sender_id)

    def _extract_qq_from_text(self, text: str) -> Set[str]:
        found: Set[str] = set()
        for m in QQ_IN_TEXT_PATTERN.finditer(text or ""):
            for g in m.groups():
                if g:
                    found.add(g)
        return found

    def _collect_related_user_ids(self, event: AstrMessageEvent) -> List[str]:
        """从 @ 组件与消息文本中的 QQ 号收集相关用户 ID"""
        related: List[str] = []
        seen: Set[str] = set()

        def add(uid: str):
            uid = str(uid).strip()
            if uid and uid not in seen:
                seen.add(uid)
                related.append(uid)

        try:
            for comp in event.get_messages():
                if isinstance(comp, At) and getattr(comp, "qq", None):
                    add(str(comp.qq))
        except Exception:
            pass

        try:
            plain_parts = []
            for comp in event.get_messages():
                if isinstance(comp, Plain) and comp.text:
                    plain_parts.append(comp.text)
            add_from_text = self._extract_qq_from_text("".join(plain_parts))
            for qq in add_from_text:
                add(qq)
        except Exception:
            pass

        if not related:
            try:
                outline = event.get_message_outline() or ""
                for qq in self._extract_qq_from_text(outline):
                    add(qq)
            except Exception:
                pass

        return related

    def _parse_field_pairs(self, match_text: str) -> List[Tuple[str, str]]:
        return [
            (field.strip(), value.strip())
            for field, value in FIELD_VALUE_PATTERN.findall(match_text)
        ]

    def _format_profile_prompt(self, template: str, **kwargs) -> str:
        try:
            return template.format(**kwargs)
        except KeyError:
            result = template
            for key, val in kwargs.items():
                result = result.replace("{" + key + "}", str(val))
            return result

    @filter.on_llm_request()
    async def add_profile_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入画像信息"""
        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)
        sender_nickname = self._get_sender_nickname(event)

        profile_summary = self.manager.format_profile_summary(user_id, session_id)
        related_ids = self._collect_related_user_ids(event)
        other_profiles_summary = self.manager.format_related_profiles(
            related_ids, session_id, exclude_user_id=user_id
        )
        allowed_fields_display = self._get_allowed_fields_display()
        max_notes_count = str(self.config.get("max_notes_count", 5))

        profile_prompt = self.config.get("profile_prompt", "")
        if profile_prompt:
            profile_prompt = self._format_profile_prompt(
                profile_prompt,
                profile_summary=profile_summary,
                allowed_fields_display=allowed_fields_display,
                max_notes_count=max_notes_count,
                sender_id=str(user_id),
                sender_nickname=sender_nickname or "未知",
                other_profiles_summary=other_profiles_summary,
            )
            req.system_prompt += f"\n{profile_prompt}"

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """解析并更新画像（支持本人 Profile 与他人 ProfileFor）"""
        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)
        contributor = self._get_contributor_label(event)
        original_text = resp.completion_text or ""

        logger.debug(
            f"[SoulMap] on_llm_resp - 用户: {user_id}, session_id: {session_id}"
        )

        if not original_text:
            return

        ops: List[Tuple[int, str, str, Optional[str], Optional[str]]] = []
        # (pos, op, target_user_id|None, field, value)

        for m in self.profile_pattern.finditer(original_text):
            for field, value in self._parse_field_pairs(m.group(1)):
                ops.append((m.start(), "update", None, field, value))

        for m in self.profile_for_pattern.finditer(original_text):
            target_id = m.group(1).strip()
            for field, value in self._parse_field_pairs(m.group(2)):
                ops.append((m.start(), "update_for", target_id, field, value))

        for m in self.delete_pattern.finditer(original_text):
            fields = [f.strip() for f in re.split(r"[,，;；、]", m.group(1)) if f.strip()]
            for field in fields:
                ops.append((m.start(), "delete", None, field, None))

        ops.sort(key=lambda x: x[0])

        self_ops: Dict[str, Tuple[str, Optional[str]]] = {}
        for_other: Dict[Tuple[str, str], Tuple[str, Optional[str]]] = {}

        for _, op, target_id, field, value in ops:
            if op == "update_for" and target_id:
                for_other[(target_id, field)] = ("update", value)
            elif op == "delete":
                self_ops[field] = ("delete", None)
            else:
                self_ops[field] = ("update", value)

        if not self_ops and not for_other:
            self._clean_profile_tags(resp, original_text)
            return

        has_changes = False

        delete_fields = [f for f, (op, _) in self_ops.items() if op == "delete"]
        digit_deletes = sorted([f for f in delete_fields if f.isdigit()], key=int, reverse=True)
        other_deletes = [f for f in delete_fields if not f.isdigit()]

        for field in other_deletes + digit_deletes:
            success, msg = self.manager.delete_field(user_id, field, session_id, save=False)
            if success:
                has_changes = True
                logger.info(f"[SoulMap] {user_id} 删除成功: {field}")
            else:
                logger.warning(f"[SoulMap] {user_id} 删除失败: {field}, {msg}")

        for field, (op_type, value) in self_ops.items():
            if op_type != "update" or value is None:
                continue
            success, msg = self.manager.update_field(
                user_id, field, value, session_id, save=False
            )
            if success:
                has_changes = True
                logger.info(f"[SoulMap] {user_id} 更新: {field}={value}")
            else:
                logger.warning(f"[SoulMap] {user_id} 更新失败: {field}={value}, {msg}")

        for (target_id, field), (op_type, value) in for_other.items():
            if op_type != "update" or value is None:
                continue
            note_contributor = contributor if field == "备注" else None
            success, msg = self.manager.update_field(
                target_id,
                field,
                value,
                session_id,
                save=False,
                contributor=note_contributor,
            )
            if success:
                has_changes = True
                logger.info(
                    f"[SoulMap] {contributor} 代录 {target_id}: {field}={value}"
                )
            else:
                logger.warning(
                    f"[SoulMap] 代录失败 {target_id}: {field}={value}, {msg}"
                )

        if has_changes:
            self.manager._save_data()

        self._clean_profile_tags(resp, original_text)

    def _clean_profile_tags(self, resp: LLMResponse, original_text: str):
        resp.completion_text = self.block_pattern.sub("", original_text).strip()
        if resp.result_chain and resp.result_chain.chain:
            for comp in resp.result_chain.chain:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = self.block_pattern.sub("", comp.text).strip()

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if result is None or not result.chain:
            return

        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                cleaned = self.block_pattern.sub("", comp.text).strip()
                if cleaned != comp.text:
                    comp.text = cleaned

    def _is_group_chat(self, event: AstrMessageEvent) -> bool:
        origin = event.unified_msg_origin or ""
        return "group" in origin.lower()

    @filter.command("我的画像")
    async def show_my_profile(self, event: AstrMessageEvent):
        if self._is_group_chat(event):
            allow_in_group = self.config.get("allow_profile_in_group", False)
            if not allow_in_group:
                denied_msg = self.config.get(
                    "group_profile_denied_msg",
                    "为保护隐私，请私聊我查看你的画像哦~",
                )
                yield event.plain_result(denied_msg)
                return

        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)

        profile = self.manager.get_user_profile(user_id, session_id)
        if not profile:
            yield event.plain_result("暂时还没有记录内容，多和我聊聊吧")
            return

        summary = self.manager.format_profile_summary(user_id, session_id)
        last_updated = profile.get("_last_updated", "未知")
        yield event.plain_result(f"📋 你的画像：\n{summary}\n\n最后更新：{last_updated}")

    @filter.command("删除画像")
    async def delete_my_field(self, event: AstrMessageEvent, field: str):
        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)
        field = field.strip()

        success, msg = self.manager.delete_field(user_id, field, session_id)
        if success:
            yield event.plain_result(f"✅ 已删除「{field}」")
        else:
            yield event.plain_result(f"❌ {msg}")

    @filter.command("清空画像")
    async def clear_my_profile(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        session_id = self._get_session_id(event)

        success = self.manager.clear_profile(user_id, session_id)
        if success:
            yield event.plain_result("✅ 已清空你的所有画像数据")
        else:
            yield event.plain_result("你还没有任何画像数据")

    @filter.command("代录画像")
    async def proxy_record_profile(
        self,
        event: AstrMessageEvent,
        target_id: str,
        field: str,
        value: str,
    ):
        """
        为他人记录画像。示例：代录画像 1346990486 对用户的称呼 曲奇
        """
        target_id = target_id.strip().lstrip("@")
        field = field.strip()
        value = value.strip()
        session_id = self._get_session_id(event)
        contributor = self._get_contributor_label(event)

        if target_id == str(event.get_sender_id()):
            yield event.plain_result("请使用正常聊天或「我的画像」管理自己的画像，代录命令用于帮助他人备注。")
            return

        note_contributor = contributor if field == "备注" else None
        success, msg = self.manager.update_field(
            target_id,
            field,
            value,
            session_id,
            contributor=note_contributor,
        )
        if success:
            yield event.plain_result(
                f"✅ 已为 QQ {target_id} 记录「{field}」：{value}\n（代录人：{contributor}）"
            )
        else:
            yield event.plain_result(f"❌ {msg}")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return event.role == "admin"

    @filter.command("查询画像")
    async def admin_query_profile(self, event: AstrMessageEvent, user_id: str):
        if not self._is_admin(event):
            yield event.plain_result(
                self.config.get(
                    "admin_permission_denied_msg", "错误：此命令仅限管理员使用。"
                )
            )
            return

        session_id = self._get_session_id(event)
        user_id = user_id.strip().lstrip("@")
        profile = self.manager.get_user_profile(user_id, session_id)

        if not profile:
            yield event.plain_result(f"用户 {user_id} 没有画像数据")
            return

        summary = self.manager.format_profile_summary(user_id, session_id)
        last_updated = profile.get("_last_updated", "未知")
        yield event.plain_result(
            f"📋 用户 {user_id} 的画像：\n{summary}\n\n最后更新：{last_updated}"
        )

    @filter.command("画像统计")
    async def admin_profile_stats(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(
                self.config.get(
                    "admin_permission_denied_msg", "错误：此命令仅限管理员使用。"
                )
            )
            return

        all_profiles = self.manager.export_all_profiles()
        user_count = len(all_profiles)

        field_counts = {}
        for profile in all_profiles.values():
            for field in profile:
                if not field.startswith("_"):
                    field_counts[field] = field_counts.get(field, 0) + 1

        response = f"📊 画像系统统计\n\n总用户数：{user_count}\n\n字段填充情况：\n"

        for field in self.manager.allowed_fields:
            count = field_counts.get(field, 0)
            rate = (count / user_count * 100) if user_count > 0 else 0
            response += f"• {field}: {count} ({rate:.1f}%)\n"

        yield event.plain_result(response)

    async def terminate(self):
        self.manager._save_data()
