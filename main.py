import os
import time
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ===== 配置 =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TZ = timezone(timedelta(hours=7))          # 需要改时区可改这里
CHECKIN_DEADLINE = "09:00:00"              # 打卡截止时间（迟到阈值）
DB_PATH = "db.sqlite3"

# ===== 报备关键字 → 分钟（按你的清单）=====
REPORT_MAP = {
    # 10 分钟
    "wc大": 10,
    "厕所大": 10,
    "大": 10,
    # 5 分钟
    "wc小": 5,
    "厕所小": 5,
    "小": 5,
    "厕所": 5,
    "wc": 5,
    "抽烟": 5,
    # 30 分钟
    "吃饭": 30,
}
# 长词优先，避免 “wc大” 被 “wc” 抢先匹配
REPORT_KEYS = sorted(REPORT_MAP.keys(), key=len, reverse=True)

# 归队触发词
RETURN_WORDS = {"1", "回", "回来了"}

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
    """去空白（含全角）、转小写，做稳健匹配"""
    t = "".join(text.split()).replace("\u3000", "")
    return t.lower()

# ===== 数据表初始化 =====
def db_init():
    conn = db_conn()
    c = conn.cursor()
    # 报备记录
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
    # 打卡记录（每日一条）
    c.execute("""
    CREATE TABLE IF NOT EXISTS checkins (
        chat_id INTEGER,
        user_id INTEGER,
        username TEXT,
        date TEXT,
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
    """执行打卡（支持：上班/打卡/到岗 文本触发）"""
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
        c.execute("INSERT INTO checkins VALUES (?,?,?,?,?)",
                  (chat_id, user.id, user.full_name, date_str, is_late))
        conn.commit()

        if is_late:
            c.execute("SELECT COUNT(*) FROM checkins WHERE chat_id=? AND user_id=? AND is_late=1",
                      (chat_id, user.id))
            late_count = c.fetchone()[0]
            await update.effective_message.reply_text(
                f"❌ 你已迟到，当前时间：{now_str}，迟到次数 {late_count}"
            )
        else:
            await update.effective_message.reply_text(
                f"✅ 打卡成功！（时间：{now_str}）"
            )
    conn.close()

# ===== 命令 =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "已就绪 ✅\n"
        "打卡：发送 “上班 / 打卡 / 到岗”\n"
        f"迟到阈值：{CHECKIN_DEADLINE}\n"
        "报备关键字：wc(5) / wc小(5) / wc大(10) / 吃饭(30) / 抽烟(5) / 大(10) / 厕所大(10) / 厕所小(5) / 小(5) / 厕所(5)\n"
        "归队：发送 “1 / 回 / 回来了”（支持空格与大小写变体）"
    )

# ===== 文本处理（打卡 / 报备 / 归队） =====
async def text_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user = update.effective_message.from_user
    raw = update.message.text.strip()
    display_name = user.full_name or user.first_name or "用户"

    # 归一化文本用于匹配
    text_norm = normalize_text(raw)

    # 1) 打卡
    if text_norm in {"上班", "打卡", "到岗"}:
        await do_checkin(update, context)
        return

    # 2) 归队（结束报备）
    if text_norm in {normalize_text(x) for x in RETURN_WORDS}:
        row = get_user_ongoing_report(chat_id, user.id)
        if not row:
            await update.effective_message.reply_text("你当前没有进行中的报备。")
            return
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
        return

    # 3) 报备（先精确，再包含；长词优先；基于标准化文本）
    hit = None
    # 先精确
    for k in REPORT_KEYS:
        if text_norm == normalize_text(k):
            hit = k
            break
    # 再包含
    if not hit:
        for k in REPORT_KEYS:
            if normalize_text(k) in text_norm:
                hit = k
                break

    if hit:
        # 若已有进行中，先提示结束
        cur = get_user_ongoing_report(chat_id, user.id)
        if cur:
            await update.effective_message.reply_text("你已有进行中的报备，请先回复 1 或“回”结束后再发起。")
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

    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_listener))

    print("✅ 机器人已启动")
    app.run_polling()

if __name__ == "__main__":
    main()
