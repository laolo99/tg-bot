# main.py  —  Railway/本地通用版
import os
import time
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========= 日志 =========
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-bot")

# ========= 基础配置 =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# 你的工作时区（示例：UTC+7）
TZ = timezone(timedelta(hours=7))
# 上班打卡“准时”阈值（默认 15:00:00，可用 Railway Variables 覆盖）
CHECKIN_DEADLINE = os.getenv("CHECKIN_DEADLINE", "15:00:00").strip()
DB_PATH = "db.sqlite3"

# ========= 报备关键字（分钟）=========
REPORT_MAP = {
    "wc大": 10, "厕所大": 10, "大": 10,
    "wc小": 5,  "厕所小": 5,  "小": 5,
    "厕所": 5,  "wc": 5,      "抽烟": 5,
    "吃饭": 30,
}
REPORT_KEYS = sorted(REPORT_MAP.keys(), key=len, reverse=True)
# 归队关键字
RETURN_WORDS = {"1", "回", "回来了"}
# 下班关键字
OFFWORK_WORDS = {"下班"}

# ========= 工具 =========
def now_local() -> datetime:
    return datetime.now(TZ)

def fmt_hms(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")

def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}小时{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"

def normalize_text(text: str) -> str:
    # 去空格（含全角）、转小写
    t = "".join(text.split()).replace("\u3000", "")
    return t.lower()

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ========= 初始化 / 自愈 =========
def db_init():
    conn = db_conn()
    c = conn.cursor()

    # 报备表
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            keyword TEXT NOT NULL,
            minutes INTEGER NOT NULL,
            start_ts INTEGER NOT NULL,
            due_ts INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ongoing','returned')) DEFAULT 'ongoing'
        )
        """
    )

    # 打卡表（含上下班时间 & 统计字段）
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS checkins (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            date TEXT,
            start_ts INTEGER,
            end_ts INTEGER,
            work_seconds INTEGER DEFAULT 0,
            is_late INTEGER,
            PRIMARY KEY(chat_id, user_id, date)
        )
        """
    )
    conn.commit()
    conn.close()

    # 为老库做字段自愈（如果之前跑过老版本）
    ensure_checkins_columns()

def ensure_checkins_columns():
    conn = db_conn()
    c = conn.cursor()
    c.execute("PRAGMA table_info(checkins)")
    cols = {row["name"] for row in c.fetchall()}
    need = {
        "start_ts": "INTEGER",
        "end_ts": "INTEGER",
        "work_seconds": "INTEGER DEFAULT 0",
        "is_late": "INTEGER",
    }
    for col, decl in need.items():
        if col not in cols:
            c.execute(f"ALTER TABLE checkins ADD COLUMN {col} {decl}")
    conn.commit()
    conn.close()

# ========= 报备逻辑 =========
def get_user_ongoing_report(chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        """
        SELECT id, keyword, minutes, start_ts, due_ts
        FROM reports
        WHERE chat_id=? AND user_id=? AND status='ongoing'
        ORDER BY start_ts DESC LIMIT 1
        """,
        (chat_id, user_id),
    )
    row = c.fetchone()
    conn.close()
    return row

def create_report(chat_id: int, user_id: int, username: str, keyword: str, minutes: int) -> int:
    now_ts = int(time.time())
    due_ts = now_ts + minutes * 60
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO reports (chat_id, user_id, username, keyword, minutes, start_ts, due_ts, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'ongoing')
        """,
        (chat_id, user_id, username or "", keyword, minutes, now_ts, due_ts),
    )
    rid = c.lastrowid
    conn.commit()
    conn.close()
    return rid

def finish_report(report_id: int):
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE reports SET status='returned' WHERE id=?", (report_id,))
    conn.commit()
    conn.close()

# ========= 上下班 =========
async def do_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_message.from_user
    chat_id = update.effective_chat.id

    now = now_local()
    now_str = fmt_hms(now)
    date_str = now.strftime("%Y-%m-%d")
    log.info("[checkin] uid=%s now=%s deadline=%s", user.id, now_str, CHECKIN_DEADLINE)

    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute(
            "SELECT 1 FROM checkins WHERE chat_id=? AND user_id=? AND date=?",
            (chat_id, user.id, date_str),
        )
        if c.fetchone():
            await update.effective_message.reply_text(
                f"{user.first_name} 今天已经打过卡了！（时间：{now_str}）"
            )
        else:
            # 字符串格式 HH:MM:SS 可直接比较
            is_late = 1 if now_str > CHECKIN_DEADLINE else 0
            c.execute(
                "INSERT INTO checkins(chat_id,user_id,username,date,start_ts,end_ts,work_seconds,is_late) VALUES (?,?,?,?,?,?,?,?)",
                (chat_id, user.id, user.full_name, date_str, int(time.time()), None, 0, is_late),
            )
            conn.commit()
            if is_late:
                await update.effective_message.reply_text(f"❌ 迟到！（时间：{now_str}）")
            else:
                await update.effective_message.reply_text(
                    f"✅ 打卡成功！又是新的一天祝你工作顺利入金不断！加油加油加油！（时间：{now_str}）"
                )
    finally:
        conn.close()

async def do_offwork(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """发送“下班”统计本日工时（上班到下班，扣除报备中时间）"""
    user = update.effective_message.from_user
    chat_id = update.effective_chat.id

    now = now_local()
    now_ts = int(time.time())
    date_str = now.strftime("%Y-%m-%d")

    try:
        conn = db_conn()
        c = conn.cursor()
        # 需有今天“上班”记录
        c.execute(
            "SELECT start_ts FROM checkins WHERE chat_id=? AND user_id=? AND date=?",
            (chat_id, user.id, date_str),
        )
        row = c.fetchone()
        if not row or not row["start_ts"]:
            await update.effective_message.reply_text("今天还没有打上班卡，不能下班哦。")
            return

        start_ts = int(row["start_ts"])

        # 统计所有“进行中”报备的总分钟（未归队也扣除）
        c.execute(
            "SELECT COALESCE(SUM(minutes*60),0) AS sec FROM reports WHERE chat_id=? AND user_id=? AND status='ongoing'",
            (chat_id, user.id),
        )
        report_seconds = int(c.fetchone()["sec"])

        work_seconds = max(0, now_ts - start_ts - report_seconds)
        work_str = fmt_duration(work_seconds)

        c.execute(
            "UPDATE checkins SET end_ts=?, work_seconds=? WHERE chat_id=? AND user_id=? AND date=?",
            (now_ts, work_seconds, chat_id, user.id, date_str),
        )
        conn.commit()

        await update.effective_message.reply_text(
            f"今日工作已结束。\n你已经工作 {work_str}\n你已经非常棒了，早点休息哦～明天再接再厉！"
        )
    finally:
        conn.close()

# ========= 指令 =========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "已就绪 ✅\n"
        "上班打卡：发送 “上班 / 打卡 / 到岗”（支持包含式，如“我到岗啦”）\n"
        "下班：发送 “下班”\n"
        f"迟到阈值：{CHECKIN_DEADLINE}\n"
        "报备关键字：wc小(5) / wc大(10) / 吃饭(30) / 抽烟(5) / 厕所(5)...\n"
        "归队：发送 “1 / 回 / 回来了”（支持空格与大小写变体）"
    )

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("pong ✅ 机器人在线")

async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.effective_message.reply_text(f"你的用户ID：{u.id}")

# ========= 文本入口（放宽匹配 + 关键日志） =========
async def text_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user = update.effective_message.from_user
    raw = update.message.text.strip()
    text_norm = normalize_text(raw)
    display_name = user.full_name or user.first_name or "用户"

    log.info("[recv] uid=%s raw=%r norm=%r", user.id, raw, text_norm)

    try:
        # 1) 上班（先精确，后包含；与报备匹配策略一致，容错更高）
        CHECKIN_KEYS = ("上班", "打卡", "到岗")
        if text_norm in {normalize_text(k) for k in CHECKIN_KEYS} \
           or any(normalize_text(k) in text_norm for k in CHECKIN_KEYS):
            await do_checkin(update, context)
            return

        # 2) 下班（支持包含）
        if text_norm in OFFWORK_WORDS or (normalize_text("下班") in text_norm):
            await do_offwork(update, context)
            return

        # 3) 归队
        if text_norm in {normalize_text(x) for x in RETURN_WORDS}:
            row = get_user_ongoing_report(chat_id, user.id)
            if not row:
                await update.effective_message.reply_text("你当前没有进行中的报备。")
                return

            report_id, keyword, minutes, start_ts, due_ts = row
            finish_report(report_id)
            used_sec = int(time.time()) - int(start_ts)
            used_str = fmt_duration(used_sec)

            if int(time.time()) > int(due_ts):
                await update.effective_message.reply_text(
                    f"{display_name} 已归队，已超时 ❌用时：{used_str}"
                )
            else:
                await update.effective_message.reply_text(
                    f"{display_name} 已归队 ✅用时：{used_str}"
                )
            return

        # 4) 发起报备（精确 → 包含，长词优先）
        hit = None
        for k in REPORT_KEYS:
            if text_norm == normalize_text(k):
                hit = k
                break
        if not hit:
            for k in REPORT_KEYS:
                if normalize_text(k) in text_norm:
                    hit = k
                    break

        if hit:
            # 若已有进行中，先要求归队
            cur = get_user_ongoing_report(chat_id, user.id)
            if cur:
                await update.effective_message.reply_text("你已有进行中的报备，请先回复 1 或“回”结束。")
                return

            mins = REPORT_MAP[hit]
            create_report(chat_id, user.id, display_name, hit, mins)
            await update.effective_message.reply_text(
                f"已报备：{hit}（{mins} 分钟）。到点请回复 1 或“回”结束。"
            )
            return

        # 5) 兜底：避免静默
        # await update.effective_message.reply_text("👀 收到，但没有匹配到任何指令。")

    except Exception as e:
        # 避免静默失败
        await update.effective_message.reply_text(f"处理消息时出错：{e!s}")

# ========= 启动钩子（清 webhook + 打印自检）=========
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)
    me = await app.bot.get_me()
    now = now_local().strftime("%Y-%m-%d %H:%M:%S")
    log.info("🚀 ONLINE as @%s(id=%s) now=%s TZ=UTC+7 DEADLINE=%s",
             me.username, me.id, now, CHECKIN_DEADLINE)

# ========= 入口 =========
def main():
    if not BOT_TOKEN:
        print("❌ 请先设置环境变量 BOT_TOKEN")
        return
    else:
        print(f"✅ BOT_TOKEN 已加载: {BOT_TOKEN[:10]}******")

    db_init()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = on_startup

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("whoami", whoami_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_listener), group=10)

    print("✅ 机器人已启动（polling）")
    app.run_polling()

if __name__ == "__main__":
    main()
