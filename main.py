from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.web import error_response, json_response, request
from datetime import datetime
import time as time_module

from . import strings, time_utils
from .datafile_manager import DatafileManager
from .user_manager import (
    BaseModelList,
    UserDataList,
    UserDataModel,
    UmoDataModel,
    UmoDataList,
    MODEL_LIST_REGISTRY,
)
from .event_utils import EventUtils
from .exceptions import AtUserCountError, PermanentRecordTimeError, TimestrValueError


PLUGIN_NAME = "astrbot_plugin_reneban"
_PERMANENT_DURATION_VALUES = {
    "0",
    "0s",
    "permanent",
    "forever",
    "infinite",
    "永远",
    "永久",
    "无期限",
}


def _parse_llm_duration(duration: str | int | float) -> int:
    """Parse the duration accepted by the LLM tool into seconds."""
    value = str(duration).strip()
    if value.lower() in _PERMANENT_DURATION_VALUES:
        return 0
    return time_utils.timestr_to_int(value)


def _duration_label(seconds: int) -> str:
    """Format a duration without the remaining-time prefix used by banlist."""
    if seconds == 0:
        return "永久"
    return time_utils.time_format(str(seconds))


class ReNeBan(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 从插件配置中获取是否启用禁用功能，默认为启用
        self.enable = bool(config.get("enable", True))
        self.llm_ban_enabled = bool(config.get("llm_ban_enabled", True))
        try:
            self.admin_umo = self._normalize_admin_umo(config.get("admin_umo", ""))
        except ValueError as exc:
            self.admin_umo = ""
            logger.warning("管理员私聊 UMO 配置无效：%s", exc)
        # 从插件配置中获取缓存存活时间，默认为60秒
        try:
            cache_ttl = max(1, int(config.get("cache_ttl", 60)))
        except (TypeError, ValueError):
            cache_ttl = 60
        MODEL_LIST_REGISTRY.start()
        # 初始化数据文件管理器
        self.data_manager = DatafileManager(
            StarTools.get_data_dir(), cache_ttl=cache_ttl
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/dashboard/data",
            self.dashboard_data,
            ["GET"],
            "ReNeBan dashboard data",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/dashboard/config",
            self.dashboard_config,
            ["POST"],
            "Save ReNeBan dashboard configuration",
        )

    @staticmethod
    def _normalize_admin_umo(value: object) -> str:
        """Normalize a configured administrator session to a private-message UMO."""
        raw = str(value or "").strip()
        if not raw:
            return ""
        if raw.count(":") == 1:
            platform_id, user_id = raw.split(":", 1)
            raw = f"{platform_id}:FriendMessage:{user_id}"
        parts = raw.split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("管理员 UMO 必须是 platform:FriendMessage:user_id 格式")
        if parts[1] != "FriendMessage":
            raise ValueError("管理员 UMO 的消息类型必须是 FriendMessage")
        return raw

    async def _notify_admin(
        self,
        event: AstrMessageEvent,
        user_id: str,
        duration: int,
        reason: str,
    ) -> None:
        """Send the LLM ban audit entry to the configured administrator."""
        if not self.admin_umo:
            logger.warning("LLM 拉黑已执行，但未配置管理员私聊 UMO")
            return

        notification = (
            "ReNeBan LLM 拉黑通知\n"
            f"用户：{user_id}\n"
            f"时长：{_duration_label(duration)}\n"
            f"原因：{reason}\n"
            f"来源会话：{event.unified_msg_origin}\n"
            f"时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}"
        )
        try:
            sent = await self.context.send_message(
                self.admin_umo,
                MessageChain().message(notification),
            )
            if not sent:
                logger.warning("管理员私聊通知未找到匹配的平台")
        except Exception:
            logger.exception("发送管理员私聊通知失败")

    def _dashboard_bans(self) -> list[dict[str, object]]:
        """Return active user bans in a JSON-friendly shape for the dashboard."""
        data = self.data_manager.get_data(["banall", "ban"])
        now = int(time_module.time())
        result: list[dict[str, object]] = []

        for item in data["banall"]:
            result.append(
                {
                    "uid": item.uid,
                    "scope": "全局",
                    "scope_id": "全局",
                    "time": "永久"
                    if item.time == 0
                    else datetime.fromtimestamp(item.time)
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S"),
                    "remaining": time_utils.timelast_format(
                        item.time - now if item.time != 0 else 0
                    ),
                    "reason": item.reason or strings.messages["no_reason"],
                    "permanent": item.time == 0,
                }
            )

        for umo, items in data["ban"].items():
            for item in items:
                result.append(
                    {
                        "uid": item.uid,
                        "scope": "会话",
                        "scope_id": umo,
                        "time": "永久"
                        if item.time == 0
                        else datetime.fromtimestamp(item.time)
                        .astimezone()
                        .strftime("%Y-%m-%d %H:%M:%S"),
                        "remaining": time_utils.timelast_format(
                            item.time - now if item.time != 0 else 0
                        ),
                        "reason": item.reason or strings.messages["no_reason"],
                        "permanent": item.time == 0,
                    }
                )

        return sorted(result, key=lambda item: (str(item["uid"]), str(item["scope"])))

    async def dashboard_data(self):
        """Return active bans and current plugin configuration."""
        return json_response(
            {
                "enabled": self.enable,
                "llm_ban_enabled": self.llm_ban_enabled,
                "cache_ttl": self.data_manager._cache_ttl,
                "admin_umo": self.admin_umo,
                "bans": self._dashboard_bans(),
            }
        )

    async def dashboard_config(self):
        """Persist configuration submitted by the plugin dashboard."""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("配置必须是 JSON 对象")

        enabled = payload.get("enabled", self.enable)
        llm_ban_enabled = payload.get("llm_ban_enabled", self.llm_ban_enabled)
        cache_ttl = payload.get("cache_ttl", self.data_manager._cache_ttl)
        if not isinstance(enabled, bool) or not isinstance(llm_ban_enabled, bool):
            return error_response("enabled 和 llm_ban_enabled 必须是布尔值")
        try:
            cache_ttl = int(cache_ttl)
        except (TypeError, ValueError):
            return error_response("cache_ttl 必须是正整数")
        if cache_ttl < 1:
            return error_response("cache_ttl 必须是正整数")

        try:
            admin_umo = self._normalize_admin_umo(
                payload.get("admin_umo", self.admin_umo)
            )
        except ValueError as exc:
            return error_response(str(exc))

        self.enable = enabled
        self.llm_ban_enabled = llm_ban_enabled
        self.admin_umo = admin_umo
        self.data_manager._cache_ttl = cache_ttl
        self.data_manager._cache_timestamp = 0
        self.config["enable"] = enabled
        self.config["llm_ban_enabled"] = llm_ban_enabled
        self.config["cache_ttl"] = cache_ttl
        self.config["admin_umo"] = admin_umo
        try:
            await self.config.save_config_async()
        except Exception:
            logger.exception("保存 ReNeBan 配置失败")
            return error_response("保存配置失败", status_code=500)
        return json_response(
            {
                "saved": True,
                "enabled": self.enable,
                "llm_ban_enabled": self.llm_ban_enabled,
                "cache_ttl": cache_ttl,
                "admin_umo": self.admin_umo,
            }
        )

    @filter.llm_tool(name="reneban_ban_user")
    async def llm_ban_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
        duration: str,
        reason: str,
    ):
        """Silently add a global user ban and notify the configured administrator.

        Args:
            user_id(string): The user ID to ban. Use the platform user ID, without an @ prefix.
            duration(string): Ban duration such as 30m, 2h, 7d, or permanent.
            reason(string): A concrete reason for the ban. This argument is required.
        """
        if not self.enable or not self.llm_ban_enabled:
            return "error: ReNeBan is disabled"

        uid = str(user_id or "").strip().lstrip("@")
        ban_reason = strings.noreason_to_none(str(reason or "").strip())
        if not uid:
            return "error: user_id is required"
        if not ban_reason:
            return "error: a concrete reason is required"
        if len(ban_reason) > 2000:
            return "error: reason is too long"
        try:
            duration_seconds = _parse_llm_duration(duration)
        except TimestrValueError:
            return "error: invalid duration; use 30m, 2h, 7d, or permanent"

        banall_list: UserDataList = self.data_manager.get_data("banall")
        try:
            if not banall_list.add_time_to_data(
                uid,
                duration_seconds,
                ban_reason,
            ):
                banall_list.append(
                    UserDataModel(
                        uid=uid,
                        time=(
                            int(time_module.time()) + duration_seconds
                            if duration_seconds != 0
                            else 0
                        ),
                        reason=ban_reason,
                    )
                )
            self.data_manager.write_data("banall", banall_list)
        except PermanentRecordTimeError:
            return "error: this user already has a permanent ban"

        await self._notify_admin(event, uid, duration_seconds, ban_reason)
        if uid == str(event.get_sender_id()):
            event.stop_event()
        # Do not expose the target, duration, or reason in the conversation.
        return None

    @filter.command("banlist")
    async def banlist(self, event: AstrMessageEvent):
        """
        显示当前群禁用名单
        """
        # 禁用功能未启用
        if not self.enable:
            group_banned_text = (
                strings.messages["group_banned_list"]
                + strings.messages["no_group_banned"]
            )
            global_banned_text = (
                strings.messages["global_banned_list"]
                + strings.messages["no_global_banned"]
            )
            group_passed_text = (
                strings.messages["group_passed_list"]
                + strings.messages["no_group_passed"]
            )
            global_passed_text = (
                strings.messages["global_passed_list"]
                + strings.messages["no_global_passed"]
            )
            umo_banned_text = (
                strings.messages["umo_banned_list"] + strings.messages["no_umo_banned"]
            )
            umo_passed_text = (
                strings.messages["umo_passed_list"] + strings.messages["no_umo_passed"]
            )
        else:
            # 获取UMO
            umo = EventUtils.get_event_umo(self.context, event)
            data: dict[str, dict[str, UserDataList] | BaseModelList] = (
                self.data_manager.get_data()
            )
            # get_pass
            try:
                group_passed_list = data["pass"][umo]
            except KeyError:
                group_passed_list = UserDataList()
            # get_pass-all
            global_passed_list: UserDataList = data["passall"]
            # get_ban
            try:
                group_banned_list = data["ban"][umo]
            except KeyError:
                group_banned_list = UserDataList()
            # get_ban-all
            global_banned_list: UserDataList = data["banall"]
            # get_pass-umo
            umo_passed_list: UmoDataList = data["umopass"]
            # get_ban-umo
            umo_banned_list: UmoDataList = data["umoban"]

            # 生成结果
            group_banned_str_list = [
                strings.messages["banlist_strlist_format"].format(
                    id=item.uid,
                    time=time_utils.timelast_format(
                        (item.time - int(time_module.time())) if item.time != 0 else 0
                    ),
                    reason=item.reason
                    if item.reason
                    else strings.messages["no_reason"],
                )
                for item in group_banned_list
            ]
            if not group_banned_str_list:
                group_banned_str_list.append(strings.messages["no_group_banned"])

            global_banned_str_list = [
                strings.messages["banlist_strlist_format"].format(
                    id=item.uid,
                    time=time_utils.timelast_format(
                        (item.time - int(time_module.time())) if item.time != 0 else 0
                    ),
                    reason=item.reason
                    if item.reason
                    else strings.messages["no_reason"],
                )
                for item in global_banned_list
            ]
            if not global_banned_str_list:
                global_banned_str_list.append(strings.messages["no_global_banned"])

            group_passed_str_list = [
                strings.messages["banlist_strlist_format"].format(
                    id=item.uid,
                    time=time_utils.timelast_format(
                        (item.time - int(time_module.time())) if item.time != 0 else 0
                    ),
                    reason=item.reason
                    if item.reason
                    else strings.messages["no_reason"],
                )
                for item in group_passed_list
            ]
            if not group_passed_str_list:
                group_passed_str_list.append(strings.messages["no_group_passed"])

            global_passed_str_list = [
                strings.messages["banlist_strlist_format"].format(
                    id=item.uid,
                    time=time_utils.timelast_format(
                        (item.time - int(time_module.time())) if item.time != 0 else 0
                    ),
                    reason=item.reason
                    if item.reason
                    else strings.messages["no_reason"],
                )
                for item in global_passed_list
            ]
            if not global_passed_str_list:
                global_passed_str_list.append(strings.messages["no_global_passed"])

            umo_banned_str_list = [
                strings.messages["banlist_strlist_format"].format(
                    id=item.umo,
                    time=time_utils.timelast_format(
                        (item.time - int(time_module.time())) if item.time != 0 else 0
                    ),
                    reason=item.reason
                    if item.reason
                    else strings.messages["no_reason"],
                )
                for item in umo_banned_list
            ]
            if not umo_banned_str_list:
                umo_banned_str_list.append(strings.messages["no_umo_banned"])

            umo_passed_str_list = [
                strings.messages["banlist_strlist_format"].format(
                    id=item.umo,
                    time=time_utils.timelast_format(
                        (item.time - int(time_module.time())) if item.time != 0 else 0
                    ),
                    reason=item.reason
                    if item.reason
                    else strings.messages["no_reason"],
                )
                for item in umo_passed_list
            ]
            if not umo_passed_str_list:
                umo_passed_str_list.append(strings.messages["no_umo_passed"])

            group_banned_text = strings.messages["group_banned_list"] + "".join(
                group_banned_str_list
            )
            global_banned_text = strings.messages["global_banned_list"] + "".join(
                global_banned_str_list
            )
            group_passed_text = strings.messages["group_passed_list"] + "".join(
                group_passed_str_list
            )
            global_passed_text = strings.messages["global_passed_list"] + "".join(
                global_passed_str_list
            )
            umo_banned_text = strings.messages["umo_banned_list"] + "".join(
                umo_banned_str_list
            )
            umo_passed_text = strings.messages["umo_passed_list"] + "".join(
                umo_passed_str_list
            )

        result = f"{group_banned_text}\n\n{global_banned_text}\n\n{group_passed_text}\n\n{global_passed_text}\n\n{umo_banned_text}\n\n{umo_passed_text}"
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban-enable")
    async def ban_enable(self, event: AstrMessageEvent):
        """
        启用禁用功能
        """
        self.enable = True
        yield event.plain_result(strings.messages["ban_enabled"])
        logger.warning(
            f"已临时启用禁用功能(in {EventUtils.get_event_umo(self.context, event)} - {event.get_sender_name()}({event.get_sender_id()}))"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban-disable")
    async def ban_disable(self, event: AstrMessageEvent):
        """
        停用禁用功能
        """
        self.enable = False
        yield event.plain_result(strings.messages["ban_disabled"])
        logger.warning(
            f"已临时禁用禁用功能(in {EventUtils.get_event_umo(self.context, event)} - {event.get_sender_name()}({event.get_sender_id()}))"
        )

    @filter.command("ban-help")
    async def ban_help(self, event: AstrMessageEvent):
        """
        显示reneban帮助信息
        """
        yield event.plain_result(strings.messages["help_text"])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban")
    async def ban_user(
        self,
        event: AstrMessageEvent,
        banuser: str,
        time: str = "0",
        reason: str | None = None,
        umo: str | None = None,
        end: str | None = None,
    ):
        """
        在会话中禁用指定用户的使用权限。
        格式：/ban <@用户|UID（QQ号）> [时间（默认无期限）] [理由（默认无理由）] [UMO]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示无期限
        示例：/ban @张三 7d
        注意：单次仅能禁用一个会话的一个用户
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("ban"))
            return
        if umo is None:
            # 若umo不存在，则使用EventUtils.get_event_umo(self.context, event)（当前群）
            umo = EventUtils.get_event_umo(self.context, event)
        reason = strings.noreason_to_none(reason)
        try:
            ban_uid: str
            event_at: str | None = EventUtils.get_event_at(event)
            if event_at:
                ban_uid = event_at
            else:
                ban_uid = banuser
        except AtUserCountError:
            yield event.plain_result(strings.command_error("ban"))
            return
        # 准备ban_user
        banlist: dict[str, UserDataList] = self.data_manager.get_data("ban")
        if banlist.get(umo) is None:
            banlist[umo] = UserDataList()
        group_banned_list: UserDataList = banlist[umo]

        try:
            update_time: int = time_utils.timestr_to_int(time)
            if not group_banned_list.add_time_to_data(
                ban_uid,
                update_time,
                reason,
            ):
                new_ban_item = UserDataModel(
                    uid=ban_uid,
                    time=(
                        (int(time_module.time()) + update_time)
                        if update_time != 0
                        else 0
                    ),
                    reason=reason,
                )
                group_banned_list.append(new_ban_item)
            self.data_manager.write_data("ban", banlist)
        except PermanentRecordTimeError:
            yield event.plain_result(
                strings.messages["time_zeroset_error"].format(command="ban")
            )
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["banned_user"].format(
                umo=umo,
                user=ban_uid,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban-all")
    async def ban_all(
        self,
        event: AstrMessageEvent,
        banuser: str,
        time: str = "0",
        reason: str | None = None,
        end: str | None = None,
    ):
        """
        在全局禁用指定用户的使用权限。
        格式：/ban-all <@用户|UID（QQ号）> [时间（默认无期限）] [理由（默认无理由）]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示无期限
        示例：/ban-all @张三 7d
        注意：单次仅能禁用一个用户
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("ban-all"))
            return
        reason = strings.noreason_to_none(reason)
        try:
            ban_uid: str
            event_at: str | None = EventUtils.get_event_at(event)
            if event_at:
                ban_uid = event_at
            else:
                ban_uid = banuser
        except AtUserCountError:
            yield event.plain_result(strings.command_error("ban-all"))
            return
        banall_list: UserDataList = self.data_manager.get_data("banall")
        try:
            update_time: int = time_utils.timestr_to_int(time)
            if not banall_list.add_time_to_data(
                ban_uid,
                update_time,
                reason,
            ):
                new_ban_item = UserDataModel(
                    uid=ban_uid,
                    time=(
                        (int(time_module.time()) + update_time)
                        if update_time != 0
                        else 0
                    ),
                    reason=reason,
                )
                banall_list.append(new_ban_item)
            self.data_manager.write_data("banall", banall_list)
        except PermanentRecordTimeError:
            yield event.plain_result(
                strings.messages["time_zeroset_error"].format(command="ban-all")
            )
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["banned_user_global"].format(
                user=ban_uid,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("pass")
    async def pass_user(
        self,
        event: AstrMessageEvent,
        passuser: str,
        time: str = "0",
        reason: str | None = None,
        umo: str | None = None,
        end: str | None = None,
    ):
        """
        在会话中解限指定用户。
        格式：/pass <@用户|UID（QQ号）> [时间（默认无期限）] [理由（默认无理由）] [UMO]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示无期限
        示例：/pass @张三 7d
        注意：单次仅能解限一个会话的一个用户
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("pass"))
            return
        if umo is None:
            # 若umo不存在，则使用EventUtils.get_event_umo(self.context, event)（当前群）
            umo = EventUtils.get_event_umo(self.context, event)
        reason = strings.noreason_to_none(reason)
        try:
            pass_uid: str
            event_at: str | None = EventUtils.get_event_at(event)
            if event_at:
                pass_uid = event_at
            else:
                pass_uid = passuser
        except AtUserCountError:
            yield event.plain_result(strings.command_error("pass"))
            return
        passlist: dict[str, UserDataList] = self.data_manager.get_data("pass")
        if passlist.get(umo) is None:
            passlist[umo] = UserDataList()
        group_passed_list: UserDataList = passlist[umo]

        try:
            update_time: int = time_utils.timestr_to_int(time)
            if not group_passed_list.add_time_to_data(
                pass_uid,
                update_time,
                reason,
            ):
                new_pass_item = UserDataModel(
                    uid=pass_uid,
                    time=(
                        (int(time_module.time()) + update_time)
                        if update_time != 0
                        else 0
                    ),
                    reason=reason,
                )
                group_passed_list.append(new_pass_item)
            self.data_manager.write_data("pass", passlist)
        except PermanentRecordTimeError:
            yield event.plain_result(
                strings.messages["time_zeroset_error"].format(command="pass")
            )
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["passed_user"].format(
                umo=umo,
                user=pass_uid,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("pass-all")
    async def pass_all(
        self,
        event: AstrMessageEvent,
        passuser: str,
        time: str = "0",
        reason: str | None = None,
        end: str | None = None,
    ):
        """
        在全局中解限指定用户。
        格式：/pass-all <@用户|UID（QQ号）> [时间（默认无期限）] [理由（默认无理由）]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示无期限
        示例：/pass-all @张三 7d
        注意：单次仅能解限一个用户
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("pass-all"))
            return
        reason = strings.noreason_to_none(reason)
        try:
            pass_uid: str
            event_at: str | None = EventUtils.get_event_at(event)
            if event_at:
                pass_uid = event_at
            else:
                pass_uid = passuser
        except AtUserCountError:
            yield event.plain_result(strings.command_error("pass-all"))
            return
        passall_list: UserDataList = self.data_manager.get_data("passall")
        try:
            update_time: int = time_utils.timestr_to_int(time)
            if not passall_list.add_time_to_data(
                pass_uid,
                update_time,
                reason,
            ):
                new_pass_item = UserDataModel(
                    uid=pass_uid,
                    time=(
                        (int(time_module.time()) + update_time)
                        if update_time != 0
                        else 0
                    ),
                    reason=reason,
                )
                passall_list.append(new_pass_item)
            self.data_manager.write_data("passall", passall_list)
        except PermanentRecordTimeError:
            yield event.plain_result(
                strings.messages["time_zeroset_error"].format(command="pass-all")
            )
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["passed_user_global"].format(
                user=pass_uid,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dec-pass")
    async def dec_pass(
        self,
        event: AstrMessageEvent,
        passuser: str,
        time: str = "0",
        reason: str | None = None,
        umo: str | None = None,
        end: str | None = None,
    ):
        """
        删除指定用户的会话解限时间。
        格式：/dec-pass <@用户|UID（QQ号）> [时间（默认无期限）] [理由（默认无理由）] [UMO]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示彻底删除解限记录
        示例：/dec-pass @张三 7d
        注意：单次仅能操作一个会话的一个用户
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("dec-pass"))
            return
        if umo is None:
            # 若umo不存在，则使用EventUtils.get_event_umo(self.context, event)（当前群）
            umo = EventUtils.get_event_umo(self.context, event)
        reason = strings.noreason_to_none(reason)
        try:
            pass_uid: str
            event_at: str | None = EventUtils.get_event_at(event)
            if event_at:
                pass_uid = event_at
            else:
                pass_uid = passuser
        except AtUserCountError:
            yield event.plain_result(strings.command_error("dec-pass"))
            return
        pass_list: dict[str, UserDataList] = self.data_manager.get_data("pass")
        group_passed_list: UserDataList = pass_list.get(umo, UserDataList())
        try:
            remove_time: int = time_utils.timestr_to_int(time)
            if not group_passed_list.subtract_time_from_data(
                pass_uid,
                remove_time,
                reason,
            ):
                yield event.plain_result(strings.messages["dec_no_record"])
                return
            self.data_manager.write_data("pass", pass_list)
        except PermanentRecordTimeError:
            yield event.plain_result(strings.messages["dec_zerotime_error"])
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["dec_passed_user"].format(
                umo=umo,
                user=pass_uid,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dec-pass-all")
    async def dec_pass_all(
        self,
        event: AstrMessageEvent,
        passuser: str,
        time: str = "0",
        reason: str | None = None,
        end: str | None = None,
    ):
        """
        删除指定用户的全局解限时间。
        格式：/dec-pass-all <@用户|UID（QQ号）> [时间（默认无期限）] [理由（默认无理由）]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示彻底删除解限记录
        示例：/dec-pass-all @张三 7d
        注意：单次仅能操作一个用户
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("dec-pass-all"))
            return
        reason = strings.noreason_to_none(reason)
        try:
            pass_uid: str
            event_at: str | None = EventUtils.get_event_at(event)
            if event_at:
                pass_uid = event_at
            else:
                pass_uid = passuser
        except AtUserCountError:
            yield event.plain_result(strings.command_error("dec-pass-all"))
            return
        passall_list: UserDataList = self.data_manager.get_data("passall")
        try:
            remove_time: int = time_utils.timestr_to_int(time)
            if not passall_list.subtract_time_from_data(
                pass_uid,
                remove_time,
                reason,
            ):
                yield event.plain_result(strings.messages["dec_no_record"])
                return
            self.data_manager.write_data("passall", passall_list)
        except PermanentRecordTimeError:
            yield event.plain_result(strings.messages["dec_zerotime_error"])
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["dec_passed_user_global"].format(
                user=pass_uid,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dec-ban")
    async def dec_ban(
        self,
        event: AstrMessageEvent,
        banuser: str,
        time: str = "0",
        reason: str | None = None,
        umo: str | None = None,
        end: str | None = None,
    ):
        """
        删除指定用户的会话封禁时间。
        格式：/dec-ban <@用户|UID（QQ号）> [时间（默认无期限）] [理由（默认无理由）] [UMO]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示彻底删除封禁记录
        示例：/dec-ban @张三 7d
        注意：单次仅能操作一个会话的一个用户
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("dec-ban"))
            return
        if umo is None:
            # 若umo不存在，则使用EventUtils.get_event_umo(self.context, event)（当前群）
            umo = EventUtils.get_event_umo(self.context, event)
        reason = strings.noreason_to_none(reason)
        try:
            ban_uid: str
            event_at: str | None = EventUtils.get_event_at(event)
            if event_at:
                ban_uid = event_at
            else:
                ban_uid = banuser
        except AtUserCountError:
            yield event.plain_result(strings.command_error("dec-ban"))
            return
        ban_list: dict[str, UserDataList] = self.data_manager.get_data("ban")
        group_banned_list: UserDataList = ban_list.get(umo, UserDataList())
        try:
            remove_time: int = time_utils.timestr_to_int(time)
            if not group_banned_list.subtract_time_from_data(
                ban_uid,
                remove_time,
                reason,
            ):
                yield event.plain_result(strings.messages["dec_no_record"])
                return
            self.data_manager.write_data("ban", ban_list)
        except PermanentRecordTimeError:
            yield event.plain_result(strings.messages["dec_zerotime_error"])
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["dec_banned_user"].format(
                user=ban_uid,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
                umo=umo,
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dec-ban-all")
    async def dec_ban_all(
        self,
        event: AstrMessageEvent,
        banuser: str,
        time: str = "0",
        reason: str | None = None,
        end: str | None = None,
    ):
        """
        删除指定用户的全局封禁时间。
        格式：/dec-ban-all <@用户|UID（QQ号）> [时间（默认无期限）] [理由（默认无理由）]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示彻底删除封禁记录
        示例：/dec-ban-all @张三 7d
        注意：单次仅能操作一个用户
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("dec-ban-all"))
            return
        reason = strings.noreason_to_none(reason)
        try:
            ban_uid: str
            event_at: str | None = EventUtils.get_event_at(event)
            if event_at:
                ban_uid = event_at
            else:
                ban_uid = banuser
        except AtUserCountError:
            yield event.plain_result(strings.command_error("dec-ban-all"))
            return
        banall_list: UserDataList = self.data_manager.get_data("banall")
        try:
            remove_time: int = time_utils.timestr_to_int(time)
            if not banall_list.subtract_time_from_data(
                ban_uid,
                remove_time,
                reason,
            ):
                yield event.plain_result(strings.messages["dec_no_record"])
                return
            self.data_manager.write_data("banall", banall_list)
        except PermanentRecordTimeError:
            yield event.plain_result(strings.messages["dec_zerotime_error"])
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["dec_banned_user_global"].format(
                user=ban_uid,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban-umo")
    async def ban_umo(
        self,
        event: AstrMessageEvent,
        umo: str,
        time: str = "0",
        reason: str | None = None,
        end: str | None = None,
    ):
        """
        禁用指定UMO的使用权限。
        格式：/ban-umo <UMO> [时间（默认无期限）] [理由（默认无理由）]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示无期限
        示例：/ban-umo napcat:GroupMessage:1145141919 7d
        注意：单次仅能禁用一个UMO
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("ban-umo"))
            return
        reason = strings.noreason_to_none(reason)
        umoban_list: UmoDataList = self.data_manager.get_data("umoban")
        try:
            update_time: int = time_utils.timestr_to_int(time)
            if not umoban_list.add_time_to_data(
                umo,
                update_time,
                reason,
            ):
                new_ban_item = UmoDataModel(
                    umo=umo,
                    time=(
                        (int(time_module.time()) + update_time)
                        if update_time != 0
                        else 0
                    ),
                    reason=reason,
                )
                umoban_list.append(new_ban_item)
            self.data_manager.write_data("umoban", umoban_list)
        except PermanentRecordTimeError:
            yield event.plain_result(
                strings.messages["time_zeroset_error"].format(command="ban-umo")
            )
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["banned_umo"].format(
                umo=umo,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("pass-umo")
    async def pass_umo(
        self,
        event: AstrMessageEvent,
        umo: str,
        time: str = "0",
        reason: str | None = None,
        end: str | None = None,
    ):
        """
        解限指定UMO的使用权限。
        格式：/pass-umo <UMO> [时间（默认无期限）] [理由（默认无理由）]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示无期限
        示例：/pass-umo napcat:GroupMessage:1145141919 7d
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("pass-umo"))
            return
        reason = strings.noreason_to_none(reason)
        umopass_list: UmoDataList = self.data_manager.get_data("umopass")
        try:
            update_time: int = time_utils.timestr_to_int(time)
            if not umopass_list.add_time_to_data(
                umo,
                update_time,
                reason,
            ):
                new_pass_item = UmoDataModel(
                    umo=umo,
                    time=(
                        (int(time_module.time()) + update_time)
                        if update_time != 0
                        else 0
                    ),
                    reason=reason,
                )
                umopass_list.append(new_pass_item)
            self.data_manager.write_data("umopass", umopass_list)
        except PermanentRecordTimeError:
            yield event.plain_result(
                strings.messages["time_zeroset_error"].format(command="pass-umo")
            )
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["passed_umo"].format(
                umo=umo,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dec-ban-umo")
    async def dec_ban_umo(
        self,
        event: AstrMessageEvent,
        umo: str,
        time: str = "0",
        reason: str | None = None,
        end: str | None = None,
    ):
        """
        删除指定UMO的封禁时间。
        格式：/dec-ban-umo <UMO> [时间（默认无期限）] [理由（默认无理由）]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示彻底删除封禁记录
        示例：/dec-ban-umo napcat:GroupMessage:1145141919 7d
        注意：单次仅能操作一个UMO
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("dec-ban-umo"))
            return
        reason = strings.noreason_to_none(reason)
        umoban_list: UmoDataList = self.data_manager.get_data("umoban")
        try:
            remove_time: int = time_utils.timestr_to_int(time)
            if not umoban_list.subtract_time_from_data(
                umo,
                remove_time,
                reason,
            ):
                yield event.plain_result(strings.messages["dec_no_record"])
                return
            self.data_manager.write_data("umoban", umoban_list)
        except PermanentRecordTimeError:
            yield event.plain_result(strings.messages["dec_zerotime_error"])
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["dec_banned_umo"].format(
                umo=umo,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dec-pass-umo")
    async def dec_pass_umo(
        self,
        event: AstrMessageEvent,
        umo: str,
        time: str = "0",
        reason: str | None = None,
        end: str | None = None,
    ):
        """
        删除指定UMO的解限时间。
        格式：/dec-pass-umo <UMO> [时间（默认无期限）] [理由（默认无理由）]
        时间格式：数字+单位（d=天，h=小时，m=分钟，s=秒），如 1d 表示1天，12h 表示12个小时，不带单位默认秒，0表示彻底删除解限记录
        示例：/dec-pass-umo napcat:GroupMessage:1145141919 7d
        注意：单次仅能操作一个UMO
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("dec-pass-umo"))
            return
        reason = strings.noreason_to_none(reason)
        umopass_list: UmoDataList = self.data_manager.get_data("umopass")
        try:
            remove_time: int = time_utils.timestr_to_int(time)
            if not umopass_list.subtract_time_from_data(
                umo,
                remove_time,
                reason,
            ):
                yield event.plain_result(strings.messages["dec_no_record"])
                return
            self.data_manager.write_data("umopass", umopass_list)
        except PermanentRecordTimeError:
            yield event.plain_result(strings.messages["dec_zerotime_error"])
            return
        except TimestrValueError as e:
            yield event.plain_result(
                strings.messages["invalid_timestr_error"].format(
                    timestr=e.invalid_timestr
                )
            )
            return

        yield event.plain_result(
            strings.messages["dec_passed_umo"].format(
                umo=umo,
                time=time_utils.time_format(time),
                reason=strings.reason_format(reason),
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban-reset")
    async def ban_reset(
        self, event: AstrMessageEvent, resetuser: str, end: str | None = None
    ):
        """
        删除一名指定用户的所有记录
        格式：/ban-reset <@用户|UID（QQ号）>
        示例：/ban-reset @张三
        注意：单次仅能操作一个用户
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("ban-reset"))
            return
        try:
            reset_uid: str
            event_at: str | None = EventUtils.get_event_at(event)
            if event_at:
                reset_uid = event_at
            else:
                reset_uid = resetuser
        except AtUserCountError:
            yield event.plain_result(strings.command_error("ban-reset"))
            return

        user_datas: dict[str, dict[str, UserDataList] | UserDataList] = (
            self.data_manager.get_data(["ban", "pass", "banall", "passall"])
        )
        for umo in list(user_datas["ban"].keys()):
            user_datas["ban"][umo].remove_by_id(reset_uid)
        for umo in list(user_datas["pass"].keys()):
            user_datas["pass"][umo].remove_by_id(reset_uid)
        user_datas["banall"].remove_by_id(reset_uid)
        user_datas["passall"].remove_by_id(reset_uid)
        self.data_manager.write_data(list(user_datas.keys()), list(user_datas.values()))

        yield event.plain_result(
            strings.messages["ban_reset_success"].format(user=reset_uid)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban-reset-umo")
    async def ban_reset_umo(
        self, event: AstrMessageEvent, umo: str, end: str | None = None
    ):
        """
        删除一名指定UMO的所有记录
        格式：/ban-reset-umo <UMO>
        示例：/ban-reset-umo napcat:GroupMessage:1145141919
        注意：单次仅能操作一个UMO
        """
        if end is not None:
            # 若end存在，说明语法错误，发送错误信息并return
            yield event.plain_result(strings.command_error("ban-reset-umo"))
            return

        umo_datas: dict[str, UmoDataList] = self.data_manager.get_data(
            ["umoban", "umopass"]
        )
        umo_datas["umoban"].remove_by_id(umo)
        umo_datas["umopass"].remove_by_id(umo)
        self.data_manager.write_data(list(umo_datas.keys()), list(umo_datas.values()))

        yield event.plain_result(
            strings.messages["ban_reset_umo_success"].format(umo=umo)
        )

    # 设置优先级，可在其他低优先级（priority<114）的命令/监听器/钩子前过滤
    @filter.event_message_type(filter.EventMessageType.ALL, priority=114)
    async def filter_banned_users(self, event: AstrMessageEvent):
        """
        全局事件过滤器：
        如果禁用功能启用且发送者被禁用，则停止事件传播，机器人不再响应该用户的消息。
        """
        if EventUtils.is_banned(self.enable, self.data_manager, self.context, event)[0]:
            event.stop_event()

    async def terminate(self):
        """可选择实现 terminate 函数，当插件被卸载/停用时会调用。"""
        MODEL_LIST_REGISTRY.stop_event.set()
