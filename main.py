# main.py — 上下班指令流 / 跨天结算 / 按实际报备用时扣减 / 旧数据自愈 / 友好语录

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
TZ = timezone(timedelta(hours=7))  # 你的工作时区（UTC+7）
CHECKIN_DEADLINE = os.getenv("CHECKIN_DEADLINE", "15:00:00").strip()  # 上班“准时”阈值（仅标记，不限制）
DB_PATH = "db.sqlite3"

# ========= 报备关键字（分钟）=========
REPORT_MAP = {
    "wc大": 10, "厕所大": 10, "大": 10,
    "wc小": 5,  "厕所小": 5,  "小": 5,
    "厕所": 5,  "wc": 5,      "抽烟": 5,
    "吃饭": 30,
}
REPORT_KEYS = sorted(REPORT_MAP.keys(), key=len, reverse=True)
RETURN_WORDS = {"1", "回", "回来了"}
OFFWORK_WORDS = {"下班"}

# ========= 工具 =========
def now_local() -> datetime:
    return datetime.now(TZ)

def fmt_hms(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")

def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")

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

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def overlap_seconds(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    """计算 [a_start,a_end) 与 [b_start,b_end) 的重叠秒数"""
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0, end - start)

def to_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def repair_legacy_open_checkins(chat_id: int, user_id: int) -> None:
    """
    修复旧数据：存在 start_ts 为 NULL/0 的记录。
    - 若 end_ts 已有：start_ts = end_ts
    - 否则：start_ts = now（从现在开始计）
    """
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        """
        SELECT date, start_ts, end_ts
        FROM checkins
        WHERE chat_id=? AND user_id=? AND (start_ts IS NULL OR start_ts=0)
        """,
        (chat_id, user_id),
    )
    rows = c.fetchall()
    if rows:
        now_ts = int(time.time())
        for r in rows:
            fix_ts = to_int(r["end_ts"], now_ts) if r["end_ts"] else now_ts
            c.execute(
                """
                UPDATE checkins
                SET start_ts=?
                WHERE chat_id=? AND user_id=? AND date=? AND (start_ts IS NULL OR start_ts=0)
                """,
                (fix_ts, chat_id, user_id, r["date"]),
            )
        conn.commit()
    conn.close()

# ========= 初始化 / 自愈 =========
def db_init():
    conn = db_conn()
    c = conn.cursor()

    # 报备表（包含 end_ts 记录“实际归队时间”）
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
            status TEXT NOT NULL CHECK(status IN ('ongoing','returned')) DEFAULT 'ongoing',
            end_ts INTEGER
        )
        """
    )

    # 打卡表
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

    ensure_checkins_columns()
    ensure_reports_columns()

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

def ensure_reports_columns():
    conn = db_conn()
    c = conn.cursor()
    c.execute("PRAGMA table_info(reports)")
    cols = {row["name"] for row in c.fetchall()}
    if "end_ts" not in cols:
        c.execute("ALTER TABLE reports ADD COLUMN end_ts INTEGER")
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
        INSERT INTO reports (chat_id, user_id, username, keyword, minutes, start_ts, due_ts, status, end_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'ongoing', NULL)
        """,
        (chat_id, user_id, username or "", keyword, minutes, now_ts, due_ts),
    )
    rid = c.lastrowid
    conn.commit()
    conn.close()
    return rid

def finish_report(report_id: int):
    now_ts = int(time.time())
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE reports SET status='returned', end_ts=? WHERE id=?",
        (now_ts, report_id),
    )
    conn.commit()
    conn.close()

# ========= 上下班 =========
async def do_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """上班/打卡/到岗：若已有未结算上班则提示；否则新开一条"""
    user = update.effective_message.from_user
    chat_id = update.effective_chat.id

    now = now_local()
    now_str = fmt_hms(now)

    try:
        # 修复旧数据
        repair_legacy_open_checkins(chat_id, user.id)

        conn = db_conn()
        c = conn.cursor()

        c.execute(
            "SELECT date, start_ts FROM checkins WHERE chat_id=? AND user_id=? AND end_ts IS NULL ORDER BY start_ts DESC LIMIT 1",
            (chat_id, user.id),
        )
        row = c.fetchone()
        if row:
            st = to_int(row["start_ts"], 0)
            if st > 0:
                await update.effective_message.reply_text(
                    f"你已在上班状态。（上次上班时间：{fmt_dt(st)}）"
                )
            else:
                conn.execute(
                    "UPDATE checkins SET start_ts=? WHERE chat_id=? AND user_id=? AND date=? AND end_ts IS NULL",
                    (int(time.time()), chat_id, user.id, row["date"]),
                )
                conn.commit()
                await update.effective_message.reply_text("已修复你上一条异常的上班记录，现在已在上班状态。")
            return

        is_late = 1 if fmt_hms(now) > CHECKIN_DEADLINE else 0  # 仅标记
        c.execute(
            "INSERT INTO checkins(chat_id,user_id,username,date,start_ts,end_ts,work_seconds,is_late) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                chat_id,
                user.id,
                user.full_name,
                now.strftime("%Y-%m-%d"),
                int(time.time()),
                None,
                0,
                is_late,
            ),
        )
        conn.commit()

        # 友好语录
        if is_late:
            await update.effective_message.reply_text(
                f"❌ 迟到打卡！（时间：{now_str}）\n"
                "今天要加油哦，调整好心态继续努力 💪"
            )
        else:
            await update.effective_message.reply_text(
                f"✅ 打卡成功！（时间：{now_str}）\n"
                "新的一天开始啦，祝你工作顺利，入金不断！加油加油加油 🚀"
            )

    finally:
        conn.close()

async def do_offwork(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    下班：找 end_ts IS NULL 的那条上班记录；
    净时长 = (now - start_ts) - 交叠的报备用时（进行中/已归队，按实际 end_ts 扣）。
    """
    user = update.effective_message.from_user
    chat_id = update.effective_chat.id

    now = now_local()
    now_ts = int(time.time())

    try:
        repair_legacy_open_checkins(chat_id, user.id)

        conn = db_conn()
        c = conn.cursor()

        c.execute(
            """
            SELECT date, start_ts
            FROM checkins
            WHERE chat_id=? AND user_id=? AND end_ts IS NULL
            ORDER BY start_ts DESC
            LIMIT 1
            """,
            (chat_id, user.id),
        )
        row = c.fetchone()
        if not row:
            await update.effective_message.reply_text("还没有上班记录，无法下班哦。")
            return

        start_ts = to_int(row["start_ts"], 0)
        start_date = row["date"]
        if start_ts <= 0:
            start_ts = now_ts
            c.execute(
                "UPDATE checkins SET start_ts=? WHERE chat_id=? AND user_id=? AND date=? AND end_ts IS NULL",
                (start_ts, chat_id, user.id, start_date),
            )
            conn.commit()

        # 报备交叠秒数（按实际 end_ts 扣；未归队最多扣到 now）
        c.execute(
            """
            SELECT start_ts, due_ts, status, end_ts
            FROM reports
            WHERE chat_id=? AND user_id=?
              AND due_ts > ? AND start_ts < ?
            """,
            (chat_id, user.id, start_ts, now_ts),
        )
        report_rows = c.fetchall()

        report_overlap_sec = 0
        for rr in report_rows:
            r_start = to_int(rr["start_ts"], 0)
            if rr["status"] == "returned" and rr["end_ts"] is not None:
                r_end = to_int(rr["end_ts"], now_ts)
            else:
                r_end = min(to_int(rr["due_ts"], now_ts), now_ts)
            if r_start > 0:
                report_overlap_sec += overlap_seconds(start_ts, now_ts, r_start, r_end)

        gross_seconds = max(0, now_ts - start_ts)
        net_seconds = max(0, gross_seconds - report_overlap_sec)

        c.execute(
            """
            UPDATE checkins
            SET end_ts=?, work_seconds=?
            WHERE chat_id=? AND user_id=? AND date=? AND start_ts=?
            """,
            (now_ts, net_seconds, chat_id, user.id, start_date, start_ts),
        )
        conn.commit()

        # 友好语录
        await update.effective_message.reply_text(
            "✅ 今日工作已结束 🎉\n"
            f"上班时间：{fmt_dt(start_ts)}\n"
            f"下班时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"总时长：{fmt_duration(gross_seconds)}\n"
            f"报备扣除：{fmt_duration(report_overlap_sec)}\n"
            f"净工作时长：{fmt_duration(net_seconds)}\n\n"
            "辛苦啦！今天的努力不会白费，早点休息，明天继续冲！🌙✨"
        )

    finally:
        conn.close()

# ========= 指令 =========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "已就绪 ✅\n"
        "上班打卡：发送 “上班 / 打卡 / 到岗”（支持包含式）\n"
        "下班：发送 “下班”（支持包含式）\n"
        f"迟到阈值：{CHECKIN_DEADLINE}\n"
        "报备关键字：wc小(5) / wc大(10) / 吃饭(30) / 抽烟(5) / 厕所(5)...\n"
        "归队：发送 “1 / 回 / 回来了”（按实际用时扣除）\n"
        "辅助：/whoami 查看你的 Telegram 用户ID"
    )

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("pong ✅ 机器人在线")

async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.effective_message.reply_text(f"你的用户ID：{u.id}")

# ========= 文本入口 =========
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
        # 1) 上班（先精确，后包含）
        CHECKIN_KEYS = ("上班", "打卡", "到岗")
        if text_norm in {normalize_text(k) for k in CHECKIN_KEYS} \
           or any(normalize_text(k) in text_norm for k in CHECKIN_KEYS):
            await do_checkin(update, context)
            return

        # 2) 下班（支持包含）
        if text_norm in OFFWORK_WORDS or (normalize_text("下班") in text_norm):
            await do_offwork(update, context)
            return

        # 3) 归队（记录实际 end_ts）
        if text_norm in {normalize_text(x) for x in RETURN_WORDS}:
            row = get_user_ongoing_report(chat_id, user.id)
            if not row:
                await update.effective_message.reply_text("你当前没有进行中的报备。")
                return

            report_id, keyword, minutes, start_ts, due_ts = row
            finish_report(report_id)  # end_ts=当前时间
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

    except Exception as e:
        await update.effective_message.reply_text(f"处理消息时出错：{e!s}")

# ========= 启动钩子 =========
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
