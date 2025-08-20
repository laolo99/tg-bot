import os
import time
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ===== 配置 =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TZ = timezone(timedelta(hours=7))          # 时区
CHECKIN_DEADLINE = "15:00:00"              # 上班打卡截止时间（下午 3 点前算准时）
DB_PATH = "db.sqlite3"

# ===== 报备关键字 → 分钟 =====
REPORT_MAP = {
    "wc大": 10,
    "厕所大": 10,
    "大": 10,
    "wc小": 5,
    "厕所小": 5,
    "小": 5,
    "厕所": 5,
    "wc": 5,
    "抽烟": 5,
    "吃饭": 30,
}
REPORT_KEYS = sorted(REPORT_MAP.keys(), key=len, reverse=True)
RETURN_WORDS = {"1", "回", "回来了"}
OFFWORK_WORDS = {"下班"}   # 下班关键字

# ===== 工具函数 =====
def db_conn():
    return sqlite3.connect(DB_PATH)

def now_local():
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
    t = "".join(text.split()).replace("\u3000", "")
    return t.lower()

# ===== 数据表初始化 =====
def db_init():
    conn = db_conn()
    c = conn.cursor()
    c.execute("""
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
    """)
    c.execute("""
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
    """)
    conn.commit()
    conn.close()

# ===== 报备逻辑 =====
def get_user_ongoing_report(chat_id: int, user_id: int):
    conn = db_conn()
    c = conn.cursor()
    c.execute("""
        SELECT id, keyword, minutes, start_ts, due_ts
        FROM reports
        WHERE chat_id=? AND user_id=? AND status='ongoing'
        ORDER BY start_ts DESC LIMIT 1
    """, (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row

def create_report(chat_id: int, user_id: int, username: str, keyword: str, minutes: int) -> int:
    now_ts = int(time.time())
    due_ts = now_ts + minutes * 60
    conn = db_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO reports (chat_id, user_id, username, keyword, minutes, start_ts, due_ts, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'ongoing')
    """, (chat_id, user_id, username or "", keyword, minutes, now_ts, due_ts))
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

# ===== 打卡逻辑 =====
async def do_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_message.from_user
    chat_id = update.effective_chat.id
    now = now_local()
    now_str = fmt_hms(now)
    date_str = now.strftime("%Y-%m-%d")

    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM checkins WHERE chat_id=? AND user_id=? AND date=?",
              (chat_id, user.id, date_str))
    row = c.fetchone()
    if row:
        await update.effective_message.reply_text(
            f"{user.first_name} 今天已经打过卡了！（时间：{now_str}）"
        )
    else:
        is_late = 1 if now_str > CHECKIN_DEADLINE else 0
        c.execute("INSERT INTO checkins VALUES (?,?,?,?,?,?,?,?)",
                  (chat_id, user.id, user.full_name, date_str, int(time.time()), None, 0, is_late))
        conn.commit()
        if is_late:
            await update.effective_message.reply_text(f"❌ 迟到！（时间：{now_str}）")
        else:
            await update.effective_message.reply_text(f"✅ 打卡成功！又是新的一天祝你工作顺利入金不断！加油，加油，加油（时间：{now_str}）")
    conn.close()

# ===== 下班逻辑 =====
async def do_offwork(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_message.from_user
    chat_id = update.effective_chat.id
    now = now_local()
    now_ts = int(time.time())
    date_str = now.strftime("%Y-%m-%d")

    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT start_ts FROM checkins WHERE chat_id=? AND user_id=? AND date=?",
              (chat_id, user.id, date_str))
    row = c.fetchone()
    if not row:
        await update.effective_message.reply_text("今天还没有打上班卡，不能下班哦。")
        conn.close()
        return

    start_ts = row[0]

    # 统计报备时间
    c.execute("SELECT SUM(minutes*60) FROM reports WHERE chat_id=? AND user_id=? AND status='ongoing'",
              (chat_id, user.id))
    report_seconds = c.fetchone()[0] or 0

    work_seconds = max(0, now_ts - start_ts - report_seconds)
    work_str = fmt_duration(work_seconds)

    c.execute("UPDATE checkins SET end_ts=?, work_seconds=? WHERE chat_id=? AND user_id=? AND date=?",
              (now_ts, work_seconds, chat_id, user.id, date_str))
    conn.commit()
    conn.close()

    await update.effective_message.reply_text(
        f"今日工作已结束。\n你已经工作 {work_str}\n你已经非常棒了，早点休息哦～明天再接再厉！"
    )

# ===== 命令 =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "已就绪 ✅\n"
        "上班打卡：发送 “上班 / 打卡 / 到岗”\n"
        "下班：发送 “下班”\n"
        f"迟到阈值：{CHECKIN_DEADLINE}\n"
        "报备关键字：wc小(5) / wc大(10) / 吃饭(30) / 抽烟(5)...\n"
        "归队：发送 “1 / 回 / 回来了”"
    )

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("pong ✅ 机器人已正常读取 BOT_TOKEN")

# ===== 文本处理 =====
async def text_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user = update.effective_message.from_user
    raw = update.message.text.strip()
    display_name = user.full_name or user.first_name or "用户"
    text_norm = normalize_text(raw)

    if text_norm in {"上班", "打卡", "到岗"}:
        await do_checkin(update, context)
        return
    if text_norm in OFFWORK_WORDS:
        await do_offwork(update, context)
        return
    if text_norm in {normalize_text(x) for x in RETURN_WORDS}:
        row = get_user_ongoing_report(chat_id, user.id)
        if row:
            report_id, keyword, minutes, start_ts, due_ts = row
            finish_report(report_id)
            used_sec = int(time.time()) - start_ts
            used_str = fmt_duration(used_sec)
            if int(time.time()) > due_ts:
                await update.effective_message.reply_text(
                    f"{display_name} 已归队，已超时 ❌用时：{used_str}"
                )
            else:
                await update.effective_message.reply_text(
                    f"{display_name} 已归队 ✅用时：{used_str}"
                )
        else:
            await update.effective_message.reply_text("你当前没有进行中的报备。")
        return

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

# ===== 入口 =====
def main():
    if not BOT_TOKEN:
        print("❌ 请先设置环境变量 BOT_TOKEN")
        return
    else:
        print(f"✅ BOT_TOKEN 已加载: {BOT_TOKEN[:10]}******")

    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_listener))
    print("✅ 机器人已启动")
    app.run_polling()

if __name__ == "__main__":
    main()
