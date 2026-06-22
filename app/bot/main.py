from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from app import config
from app.storage import atomic_write_json, load_json

ASK_REMARK, ASK_HOST_TYPE, ASK_CREATE_DATE, ASK_CREATE_DATE_MANUAL = range(4)
DEL_AWAIT_NUMBER = 4
RENAME_AWAIT_NUMBER, RENAME_AWAIT_REMARK, RENAME_AWAIT_DATE, RENAME_AWAIT_DATE_MANUAL = range(5, 9)

logger = logging.getLogger(__name__)
user_data: dict[str, Any] = {}


def setup_logging() -> None:
    config.ensure_runtime_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def load_user_data() -> dict[str, Any]:
    data = load_json(config.USER_DATA_FILE, {})
    return data if isinstance(data, dict) else {}


def save_user_data() -> None:
    atomic_write_json(config.USER_DATA_FILE, user_data, mode=0o600)


def get_user_record(user_id: str) -> dict[str, Any]:
    record = user_data.setdefault(user_id, {})
    record.setdefault("machines", [])
    return record


def unblock_user(user_id: str) -> None:
    record = get_user_record(user_id)
    if record.get("is_blocked"):
        record["is_blocked"] = False
        save_user_data()


def block_user(user_id: str) -> None:
    get_user_record(user_id)["is_blocked"] = True
    save_user_data()


def calculate_expiration_time(machine: dict[str, Any]) -> datetime:
    event_date = datetime.strptime(machine["last_event_date"], "%Y-%m-%d")
    return event_date.replace(hour=1, minute=0, second=0, microsecond=0) + timedelta(days=int(machine["renewal_days"]))


def format_timedelta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "已过期"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def parse_date_input(text: str) -> datetime | None:
    text = text.strip()
    now = datetime.now()
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
            return datetime.strptime(text, "%Y-%m-%d")
        if re.fullmatch(r"\d{1,2}-\d{1,2}", text):
            month, day = map(int, text.split("-"))
            return datetime(now.year, month, day)
    except ValueError:
        return None
    return None


def parse_multi_selection(selection: str, max_num: int) -> list[int]:
    selected: set[int] = set()
    for part in selection.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = map(int, part.split("-", 1))
            if start > end:
                start, end = end, start
            selected.update(range(start, end + 1))
        else:
            selected.add(int(part))
    return sorted({i - 1 for i in selected if 1 <= i <= max_num})


def is_remark_duplicate(user_id: str, remark: str, exclude_uuid: str | None = None) -> bool:
    for machine in get_user_record(user_id).get("machines", []):
        if machine.get("remark") == remark and machine.get("uuid") != exclude_uuid:
            return True
    return False


def load_snapshot() -> dict[str, Any] | None:
    snapshot = load_json(config.SNAPSHOT_JSON_FILE, None)
    if isinstance(snapshot, dict) and snapshot.get("centers"):
        return snapshot
    # 兼容旧 txt 数据。
    try:
        lines = config.SNAPSHOT_TEXT_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        try:
            lines = config.LEGACY_TEST_FILE.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None
    centers: dict[str, int] = {}
    total = 0
    updated_at = ""
    for line in lines:
        if "更新于:" in line:
            updated_at = line.split("更新于:", 1)[1].strip(" -")
        if "在线VPS总数" in line:
            m = re.search(r"\d+", line)
            if m:
                total = int(m.group())
        if line.startswith("✅ 数据中心:") and "VPS 数量:" in line:
            try:
                name = line.split("✅ 数据中心:", 1)[1].split(",", 1)[0].strip()
                count = int(re.search(r"VPS 数量:\s*(\d+)", line).group(1))  # type: ignore[union-attr]
                centers[name] = count
            except Exception:
                continue
    if not total and centers:
        total = sum(centers.values())
    if not centers:
        return None
    return {"updated_at": updated_at, "total": total, "centers": centers}


def format_snapshot(snapshot: dict[str, Any]) -> str:
    updated_at = snapshot.get("updated_at") or "未知"
    total = snapshot.get("total") or 0
    centers = snapshot.get("centers") or {}
    lines = [f"📊 HAX 数据中心状态", f"更新时间：{updated_at}", f"在线VPS总数：{total}", ""]
    for name, count in sorted(centers.items(), key=lambda item: item[0].lower()):
        lines.append(f"• {name}: {count}")
    return "\n".join(lines)


def get_help_text() -> str:
    return (
        "欢迎使用 HAX BOT 7.9\n\n"
        "常用命令：\n"
        "/new - 添加机器续期提醒\n"
        "/info - 查看机器列表和剩余时间\n"
        "/rename - 修改备注或续期日期\n"
        "/delmachine - 删除机器，支持 1,3 或 1-3\n"
        "/monitor - 开启/关闭 HAX 数据中心变化提醒\n"
        "/status - 查看当前采集到的数据中心状态\n"
        "/interval - 查看或修改采集间隔，例如 /interval 60\n"
        "/cancel - 取消当前操作"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        unblock_user(str(update.effective_user.id))
    await update.effective_message.reply_text(get_help_text())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(get_help_text())



async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    snapshot = load_snapshot()
    if not snapshot:
        await update.effective_message.reply_text("暂时没有采集数据，请稍后查看 collector 日志。")
        return
    await update.effective_message.reply_text(format_snapshot(snapshot))


def build_interval_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("30秒", callback_data="interval_set_30"),
                InlineKeyboardButton("60秒", callback_data="interval_set_60"),
                InlineKeyboardButton("120秒", callback_data="interval_set_120"),
            ],
            [
                InlineKeyboardButton("300秒", callback_data="interval_set_300"),
                InlineKeyboardButton("600秒", callback_data="interval_set_600"),
            ],
            [InlineKeyboardButton("🔄 刷新当前间隔", callback_data="interval_refresh")],
        ]
    )


def format_interval_help() -> str:
    interval = config.get_interval_seconds()
    return (
        f"⏱ 当前采集间隔：{interval} 秒\n\n"
        "修改方式：\n"
        "1. 直接发送：/interval 60\n"
        "2. 或点击下面常用间隔按钮\n\n"
        f"允许范围：{config.MIN_INTERVAL_SECONDS} - {config.MAX_INTERVAL_SECONDS} 秒。"
    )


def restart_collector_service() -> str:
    service_name = os.getenv("HAX_COLLECTOR_SERVICE", "hax-bot-collector.service")
    if os.getenv("HAX_SKIP_SYSTEMCTL", "").strip() == "1":
        return "已写入配置；当前设置为跳过 systemctl，采集器下次读取配置时生效。"
    if not shutil.which("systemctl"):
        return "已写入配置；当前环境没有 systemctl，采集器下次读取配置时生效。"
    try:
        subprocess.run(
            ["systemctl", "restart", service_name],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return f"已重启 {service_name}，新间隔立即生效。"
    except Exception as exc:  # noqa: BLE001
        logger.warning("重启采集服务失败：%s", exc)
        return "配置已写入，但自动重启采集服务失败；可手动执行 systemctl restart hax-bot-collector。"


def reschedule_datacenter_job(context: ContextTypes.DEFAULT_TYPE, interval: int) -> None:
    if not context.job_queue:
        return
    for job in context.job_queue.get_jobs_by_name("datacenter_watch"):
        job.schedule_removal()
    context.job_queue.run_repeating(
        check_datacenters_job,
        interval=max(config.MIN_INTERVAL_SECONDS, interval),
        first=5,
        name="datacenter_watch",
    )


async def apply_interval(update: Update, context: ContextTypes.DEFAULT_TYPE, value: int, *, edit_message: bool = False) -> None:
    interval = config.write_interval_seconds(value)
    reschedule_datacenter_job(context, interval)
    restart_msg = restart_collector_service()
    text = f"✅ 采集间隔已修改为：{interval} 秒\n{restart_msg}"
    if edit_message and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=build_interval_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=build_interval_keyboard())


async def interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        raw = context.args[0].strip()
        if not raw.isdigit():
            await update.effective_message.reply_text("格式错误。示例：/interval 60")
            return
        value = int(raw)
        if value < config.MIN_INTERVAL_SECONDS or value > config.MAX_INTERVAL_SECONDS:
            await update.effective_message.reply_text(
                f"间隔范围必须是 {config.MIN_INTERVAL_SECONDS} - {config.MAX_INTERVAL_SECONDS} 秒。"
            )
            return
        await apply_interval(update, context, value)
        return

    await update.effective_message.reply_text(format_interval_help(), reply_markup=build_interval_keyboard())


async def interval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "interval_refresh":
        await query.edit_message_text(format_interval_help(), reply_markup=build_interval_keyboard())
        return
    try:
        value = int(query.data.rsplit("_", 1)[1])
    except Exception:
        await query.edit_message_text("间隔参数无效，请使用 /interval 60 修改。")
        return
    await apply_interval(update, context, value, edit_message=True)


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    unblock_user(user_id)
    machines = get_user_record(user_id).get("machines", [])
    if not machines:
        await update.message.reply_text("您还没有机器。使用 /new 添加。")
        return
    now = datetime.now()
    lines = ["您的机器列表："]
    for i, machine in enumerate(machines, start=1):
        exp_dt = calculate_expiration_time(machine)
        time_left = exp_dt - now
        host_name = config.HOST_TYPES.get(machine.get("host_type"), {}).get("name", machine.get("host_type", "未知"))
        lines.append(
            f"{i}. 「{machine['remark']}」[{host_name}]\n"
            f"   最近日期：{machine['last_event_date']}\n"
            f"   过期时间：{exp_dt:%Y-%m-%d %H:%M}\n"
            f"   剩余：{format_timedelta(time_left)}"
        )
    await update.message.reply_text("\n".join(lines))


async def new_machine_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    unblock_user(str(update.effective_user.id))
    context.user_data.clear()
    await update.message.reply_text("请输入这台机器的备注：")
    return ASK_REMARK


async def received_remark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    remark = update.message.text.strip()
    if not remark:
        await update.message.reply_text("备注不能为空，请重新输入：")
        return ASK_REMARK
    if is_remark_duplicate(user_id, remark):
        await update.message.reply_text("⚠️ 备注名已存在，请换一个：")
        return ASK_REMARK
    context.user_data["remark"] = remark
    buttons = [[InlineKeyboardButton(v["name"], callback_data=f"host_{k}")] for k, v in config.HOST_TYPES.items()]
    await update.message.reply_text("请选择主机类型：", reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_HOST_TYPE


async def received_host_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    host_type = query.data.split("_", 1)[1]
    context.user_data["host_type"] = host_type
    keyboard = [
        [InlineKeyboardButton("今天", callback_data="create_today")],
        [InlineKeyboardButton("昨天", callback_data="create_yesterday")],
        [InlineKeyboardButton("手动输入日期", callback_data="create_manual")],
    ]
    await query.edit_message_text(
        f"已选：{config.HOST_TYPES[host_type]['name']}\n请选择创建/续期日期：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_CREATE_DATE


async def received_creation_date_option(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data
    if choice == "create_manual":
        await query.edit_message_text("请输入日期，格式 MM-DD 或 YYYY-MM-DD：")
        return ASK_CREATE_DATE_MANUAL
    creation_date = datetime.now() if choice == "create_today" else datetime.now() - timedelta(days=1)
    context.user_data["creation_date"] = creation_date
    return await finish_adding_machine(update, context)


async def received_creation_date_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    creation_date = parse_date_input(update.message.text)
    if not creation_date:
        await update.message.reply_text("日期格式错误，请输入 MM-DD 或 YYYY-MM-DD，或 /cancel 取消。")
        return ASK_CREATE_DATE_MANUAL
    context.user_data["creation_date"] = creation_date
    return await finish_adding_machine(update, context)


async def finish_adding_machine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    host_type = context.user_data["host_type"]
    creation_date: datetime = context.user_data["creation_date"]
    machine = {
        "uuid": str(uuid.uuid4()),
        "remark": context.user_data["remark"],
        "host_type": host_type,
        "renewal_days": config.HOST_TYPES[host_type]["days"],
        "last_event_date": creation_date.strftime("%Y-%m-%d"),
        "last_hourly_reminder_sent": None,
    }
    get_user_record(user_id).setdefault("machines", []).append(machine)
    save_user_data()
    exp_dt = calculate_expiration_time(machine)
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ 机器「{machine['remark']}」添加成功！\n"
            f"创建/续期日期：{creation_date:%Y-%m-%d}\n"
            f"预计过期：{exp_dt:%Y-%m-%d %H:%M}"
        ),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def delete_machine_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    machines = get_user_record(user_id).get("machines", [])
    if not machines:
        await update.message.reply_text("您没有可删除的机器。")
        return ConversationHandler.END
    lines = ["请选择要删除的机器序号，支持多选：", "示例：1,3 或 1-3", ""]
    lines += [f"{i}. 「{m['remark']}」" for i, m in enumerate(machines, start=1)]
    await update.message.reply_text("\n".join(lines))
    return DEL_AWAIT_NUMBER


async def received_delete_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    machines = get_user_record(user_id).get("machines", [])
    try:
        selected = parse_multi_selection(update.message.text, len(machines))
        if not selected:
            raise ValueError("empty selection")
    except Exception:
        await update.message.reply_text("格式错误，请按 1,3 或 1-3 输入，或 /cancel 取消。")
        return DEL_AWAIT_NUMBER
    deleted: list[str] = []
    for idx in sorted(selected, reverse=True):
        deleted.append(machines[idx]["remark"])
        del machines[idx]
    save_user_data()
    await update.message.reply_text("🗑️ 已删除：\n" + "\n".join(f"• {name}" for name in deleted))
    return ConversationHandler.END


async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    unblock_user(user_id)
    context.user_data.clear()
    machines = get_user_record(user_id).get("machines", [])
    if not machines:
        await update.message.reply_text("您还没有机器。使用 /new 添加。")
        return ConversationHandler.END
    lines = ["请选择要修改的机器序号：", ""] + [f"{i}. 「{m['remark']}」" for i, m in enumerate(machines, start=1)]
    await update.message.reply_text("\n".join(lines))
    return RENAME_AWAIT_NUMBER


async def received_rename_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    machines = get_user_record(user_id).get("machines", [])
    try:
        idx = int(update.message.text.strip()) - 1
        if idx < 0 or idx >= len(machines):
            raise ValueError
    except Exception:
        await update.message.reply_text("无效序号，请重新输入或 /cancel。")
        return RENAME_AWAIT_NUMBER
    context.user_data["rename_index"] = idx
    await update.message.reply_text("请输入新的备注名：")
    return RENAME_AWAIT_REMARK


async def received_rename_remark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    idx = context.user_data["rename_index"]
    machine = get_user_record(user_id)["machines"][idx]
    new_remark = update.message.text.strip()
    if not new_remark:
        await update.message.reply_text("备注不能为空，请重新输入：")
        return RENAME_AWAIT_REMARK
    if is_remark_duplicate(user_id, new_remark, exclude_uuid=machine["uuid"]):
        await update.message.reply_text("⚠️ 备注名已存在，请换一个：")
        return RENAME_AWAIT_REMARK
    context.user_data["new_remark"] = new_remark
    keyboard = [
        [InlineKeyboardButton("今天", callback_data="rename_today")],
        [InlineKeyboardButton("昨天", callback_data="rename_yesterday")],
        [InlineKeyboardButton("手动输入日期", callback_data="rename_manual")],
        [InlineKeyboardButton("仅修改备注，不更新日期", callback_data="rename_no_update")],
    ]
    await update.message.reply_text("请选择续期日期：", reply_markup=InlineKeyboardMarkup(keyboard))
    return RENAME_AWAIT_DATE


async def received_renew_date_option(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data
    if choice == "rename_manual":
        await query.edit_message_text("请输入新的续期日期，格式 MM-DD 或 YYYY-MM-DD：")
        return RENAME_AWAIT_DATE_MANUAL
    if choice == "rename_no_update":
        return await finish_rename(update, context, update_date=False)
    renew_date = datetime.now() if choice == "rename_today" else datetime.now() - timedelta(days=1)
    context.user_data["renew_date"] = renew_date
    return await finish_rename(update, context, update_date=True)


async def received_renew_date_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    renew_date = parse_date_input(update.message.text)
    if not renew_date:
        await update.message.reply_text("日期格式错误，请输入 MM-DD 或 YYYY-MM-DD，或 /cancel 取消。")
        return RENAME_AWAIT_DATE_MANUAL
    context.user_data["renew_date"] = renew_date
    return await finish_rename(update, context, update_date=True)


async def finish_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, update_date: bool) -> int:
    user_id = str(update.effective_user.id)
    idx = context.user_data["rename_index"]
    machine = get_user_record(user_id)["machines"][idx]
    machine["remark"] = context.user_data["new_remark"]
    if update_date:
        renew_date: datetime = context.user_data["renew_date"]
        machine["last_event_date"] = renew_date.strftime("%Y-%m-%d")
        machine["last_hourly_reminder_sent"] = None
    save_user_data()
    exp_dt = calculate_expiration_time(machine)
    text = f"✅ 修改成功！\n备注：{machine['remark']}\n过期时间：{exp_dt:%Y-%m-%d %H:%M}"
    if update.callback_query:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    context.user_data.clear()
    return ConversationHandler.END


async def renew_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    machine_uuid = query.data.split("_", 1)[1]
    for machine in get_user_record(user_id).get("machines", []):
        if machine.get("uuid") == machine_uuid:
            machine["last_event_date"] = datetime.now().strftime("%Y-%m-%d")
            machine["last_hourly_reminder_sent"] = None
            save_user_data()
            exp_dt = calculate_expiration_time(machine)
            await query.edit_message_text(f"✅ 机器「{machine['remark']}」已续期！\n新过期时间：{exp_dt:%Y-%m-%d %H:%M}")
            return
    await query.edit_message_text("❌ 未找到对应机器，可能已经删除。")


async def monitor_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    unblock_user(user_id)
    record = get_user_record(user_id)
    enabled = bool(record.get("dc_monitor_enabled"))
    last_total = record.get("last_dc_total_count", "N/A")
    text = f"📊 数据中心变化监控\n\n当前状态：{'✅ 已开启' if enabled else '❌ 已关闭'}\n上次记录总数：{last_total}"
    keyboard = [
        [InlineKeyboardButton("❌ 关闭监控" if enabled else "✅ 开启监控", callback_data="toggle_dc_monitor")],
        [InlineKeyboardButton("🔄 手动刷新基线", callback_data="dc_manual_refresh")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def toggle_dc_monitor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    record = get_user_record(user_id)
    record["dc_monitor_enabled"] = not bool(record.get("dc_monitor_enabled"))
    if record["dc_monitor_enabled"]:
        snapshot = load_snapshot()
        if snapshot:
            record["last_dc_stats"] = snapshot.get("centers") or {}
            record["last_dc_total_count"] = snapshot.get("total") or 0
    save_user_data()
    await monitor_command(update, context)


async def manual_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("正在刷新基线...")
    user_id = str(query.from_user.id)
    snapshot = load_snapshot()
    if not snapshot:
        await query.message.reply_text("❌ 暂时没有有效采集数据，请稍后再试。")
        return
    record = get_user_record(user_id)
    record["last_dc_stats"] = snapshot.get("centers") or {}
    record["last_dc_total_count"] = snapshot.get("total") or 0
    save_user_data()
    await query.message.reply_text("🔄 基线已刷新：\n\n" + format_snapshot(snapshot))


async def check_datacenters_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    snapshot = load_snapshot()
    if not snapshot:
        return
    current_stats = snapshot.get("centers") or {}
    current_total = int(snapshot.get("total") or 0)
    if not current_stats:
        return

    changed = False
    for user_id, record in list(user_data.items()):
        if record.get("is_blocked") or not record.get("dc_monitor_enabled"):
            continue
        last_stats = record.get("last_dc_stats") or {}
        last_total = int(record.get("last_dc_total_count") or 0)
        if not last_stats:
            record["last_dc_stats"] = current_stats
            record["last_dc_total_count"] = current_total
            changed = True
            continue

        changes: list[str] = []
        if last_total and current_total and last_total != current_total:
            changes.append(f"总数：{last_total} → {current_total}")
        for name in sorted(set(last_stats) | set(current_stats)):
            old = int(last_stats.get(name) or 0)
            new = int(current_stats.get(name) or 0)
            if old != new:
                changes.append(f"{name}: {old} → {new}")

        if changes:
            check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            msg = (
                f"🔔 HAX 数据中心变化提醒\n"
                f"检测时间：{check_time}\n\n"
                + "\n".join(f"• {line}" for line in changes)
            )
            try:
                await context.bot.send_message(chat_id=int(user_id), text=msg)
            except Forbidden:
                block_user(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("发送数据中心提醒失败 user=%s: %s", user_id, exc)

        record["last_dc_stats"] = current_stats
        record["last_dc_total_count"] = current_total
        changed = True

    if changed:
        save_user_data()


async def check_expirations_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    changed = False
    for user_id, record in list(user_data.items()):
        if record.get("is_blocked"):
            continue
        for machine in record.get("machines", []):
            try:
                exp_dt = calculate_expiration_time(machine)
            except Exception:
                continue
            time_left = exp_dt - now
            if not (timedelta(0) < time_left <= timedelta(days=2)):
                continue
            last_sent_iso = machine.get("last_hourly_reminder_sent")
            last_sent = datetime.fromisoformat(last_sent_iso) if last_sent_iso else None
            if last_sent and now - last_sent < timedelta(hours=6):
                continue
            text = f"⏳ 您的机器「{machine['remark']}」还剩 {format_timedelta(time_left)} 即将过期。"
            buttons = [[InlineKeyboardButton("✅ 我已续期", callback_data=f"renew_{machine['uuid']}")]]
            try:
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text=text,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
                machine["last_hourly_reminder_sent"] = now.isoformat()
                changed = True
            except Forbidden:
                block_user(user_id)
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("发送续期提醒失败 user=%s: %s", user_id, exc)
    if changed:
        save_user_data()


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("操作已取消。")
    return ConversationHandler.END


def build_application(token: str) -> Application:
    http_request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=30)
    application = Application.builder().token(token).request(http_request).build()

    conv_new = ConversationHandler(
        entry_points=[CommandHandler("new", new_machine_command)],
        states={
            ASK_REMARK: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_remark)],
            ASK_HOST_TYPE: [CallbackQueryHandler(received_host_type, pattern=r"^host_")],
            ASK_CREATE_DATE: [CallbackQueryHandler(received_creation_date_option, pattern=r"^create_")],
            ASK_CREATE_DATE_MANUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_creation_date_manual)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )
    conv_del = ConversationHandler(
        entry_points=[CommandHandler("delmachine", delete_machine_command)],
        states={DEL_AWAIT_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_delete_number)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )
    conv_rename = ConversationHandler(
        entry_points=[CommandHandler("rename", rename_command)],
        states={
            RENAME_AWAIT_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_rename_number)],
            RENAME_AWAIT_REMARK: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_rename_remark)],
            RENAME_AWAIT_DATE: [CallbackQueryHandler(received_renew_date_option, pattern=r"^rename_")],
            RENAME_AWAIT_DATE_MANUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_renew_date_manual)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        conversation_timeout=300,
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("interval", interval_command))
    application.add_handler(CommandHandler("setinterval", interval_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("monitor", monitor_command))
    application.add_handler(conv_new)
    application.add_handler(conv_del)
    application.add_handler(conv_rename)
    application.add_handler(CallbackQueryHandler(renew_button_callback, pattern=r"^renew_"))
    application.add_handler(CallbackQueryHandler(toggle_dc_monitor_callback, pattern=r"^toggle_dc_monitor$"))
    application.add_handler(CallbackQueryHandler(manual_refresh_callback, pattern=r"^dc_manual_refresh$"))
    application.add_handler(CallbackQueryHandler(interval_callback, pattern=r"^interval_"))

    interval = max(config.MIN_INTERVAL_SECONDS, config.get_interval_seconds())
    application.job_queue.run_repeating(check_expirations_job, interval=60, first=10)
    application.job_queue.run_repeating(check_datacenters_job, interval=interval, first=15, name="datacenter_watch")
    return application


def main() -> None:
    global user_data
    setup_logging()
    config.ensure_runtime_dirs()
    user_data = load_user_data()
    token = config.get_token()
    if not token or ":" not in token:
        logger.critical("未找到有效 Telegram Bot Token，请检查 token.txt 或 HAX_TOKEN 环境变量")
        raise SystemExit(1)
    logger.info("HAX BOT 启动中...")
    application = build_application(token)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
