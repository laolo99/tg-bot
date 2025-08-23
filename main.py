# main.py — 最终版
# 规则：
# 上班：当天仅允许一次；迟到按 CHECKIN_DEADLINE 判定并累计次数；重复则提示“今天已打过卡”。
# 下班：净工时 = 下班 - 上班 - 报备实际用时(end_ts)。
# 报备：记录 start_ts/due_ts，归队时写 end_ts，按实际用时扣。
# 稳定性：SQLite WAL + 超时 + 用户级 asyncio.Lock，避免 database is locked。

import os
import time
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ========= 配置 =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TZ = timezone(timedelta(hours=7))  # 你的工作时区
CHECKIN_DEADLINE = os.getenv("CHECKIN_DEADLINE", "15:00:00").strip()
RESET_HOURS = int(os.getenv("RESET_HOURS", "12"))  # 超过 N 小时视为新的一天（用于自动清残留）

DB_PATH = "db.sqlite3"

REPORT_MAP = {
    "wc大": 10, "厕所大": 10, "大": 10,
    "wc小": 5,  "厕所小": 5,  "小": 5,
    "厕所": 5,  "wc": 5,      "抽烟": 5,
    "吃饭": 30,
}
RETURN_WORDS = {"1", "回", "回来了"}
OFFWORK_WORDS = {"下班"}
REPORT_KEYS = sorted(REPORT_MAP.keys(), key=len, reverse=True)

# ========= 小工具 =========
_locks = {}  # (chat_id, user_id) -> asyncio.Lock
def get_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    key = (chat_id, user_id)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]

def now_local() -> datetime:
    return datetime.now(TZ)

def fmt_hms(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")

def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")

def fmt_duration(sec: int) -> str:
    s = max(0, int(sec)); m, s = divmod(s, 60); h, m = divmod(m, 60)
    if h: return f"{h}小时{m}分{s}秒"
    if m: return f"{m}分{s}秒"
    return f"{s}秒"

def normalize_text(text: str) -> str:
    return "".join(text.split()).replace("\u3000", "").lower()

def overlap_seconds(a1: int, a2: int, b1: int, b2: int) -> int:
    return max(0, min(a2, b2) - max(a1, b1))

def to_int(x, default=0) -> int:
    try: return int(x)
    except Exception: return default

def db_conn():
    # 30s 等锁，允许跨线程
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ========= 初始化 / 自愈 =========
def db_init():
    conn = db_conn(); c = conn.cursor()
    # 报备
    c.execute("""
        CREATE TABLE IF NOT EXISTS reports(
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
    """)
    # 打卡（date=自然日 YYYY-MM-DD；当天仅一条，避免 UNIQUE 冲突）
    c.execute("""
        CREATE TABLE IF NOT EXISTS checkins(
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            date TEXT,             -- 自然日 YYYY-MM-DD
            start_ts INTEGER,
            end_ts INTEGER,
            work_seconds INTEGER DEFAULT 0,
            is_late INTEGER,
            PRIMARY KEY(chat_id, user_id, date)
        )
    """)
    # 统计（累计迟到次数）
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats(
            chat_id INTEGER,
            user_id INTEGER,
            late_count INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        )
    """)
    conn.commit()
    # 并发优化
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    conn.commit(); conn.close()

def ensure_stats_row(chat_id: int, user_id: int):
    conn=db_conn(); c=conn.cursor()
    c.execute("INSERT OR IGNORE INTO stats(chat_id,user_id,late_count) VALUES(?,?,0)", (chat_id,user_id))
    conn.commit(); conn.close()

def inc_late_count(chat_id: int, user_id: int) -> int:
    ensure_stats_row(chat_id, user_id)
    conn=db_conn(); c=conn.cursor()
    c.execute("UPDATE stats SET late_count=late_count+1 WHERE chat_id=? AND user_id=?", (chat_id,user_id))
    conn.commit()
    c.execute("SELECT late_count FROM stats WHERE chat_id=? AND user_id=?", (chat_id,user_id))
    n = to_int(c.fetchone()["late_count"], 0)
    conn.close()
    return n

def get_late_count(chat_id:int, user_id:int) -> int:
    ensure_stats_row(chat_id, user_id)
    conn=db_conn(); c=conn.cursor()
    c.execute("SELECT late_count FROM stats WHERE chat_id=? AND user_id=?", (chat_id,user_id))
    n = to_int(c.fetchone()["late_count"], 0)
    conn.close()
    return n

def clean_legacy_open_checkins(chat_id:int, user_id:int):
    """超过 RESET_HOURS 的未下班记录自动闭合（0 工时），避免挡住新打卡。"""
    conn=db_conn(); c=conn.cursor()
    c.execute("""SELECT date, start_ts FROM checkins
                 WHERE chat_id=? AND user_id=? AND end_ts IS NULL""", (chat_id,user_id))
    rows=c.fetchall()
    if rows:
        now_ts=int(time.time())
        for r in rows:
            st=to_int(r["start_ts"],0)
            if st and now_ts - st > RESET_HOURS*3600:
                c.execute("""UPDATE checkins SET end_ts=?, work_seconds=?
                             WHERE chat_id=? AND user_id=? AND date=? AND start_ts=? AND end_ts IS NULL""",
                          (st, 0, chat_id, user_id, r["date"], st))
    conn.commit(); conn.close()

# ========= 报备 =========
def get_user_ongoing_report(chat_id:int, user_id:int) -> Optional[sqlite3.Row]:
    conn=db_conn(); c=conn.cursor()
    c.execute("""SELECT id, keyword, minutes, start_ts, due_ts
                 FROM reports
                 WHERE chat_id=? AND user_id=? AND status='ongoing'
                 ORDER BY start_ts DESC LIMIT 1""", (chat_id,user_id))
    row=c.fetchone(); conn.close(); return row

def create_report(chat_id:int,user_id:int,username:str,keyword:str,minutes:int)->int:
    now_ts=int(time.time()); due=now_ts+minutes*60
    conn=db_conn(); c=conn.cursor()
    c.execute("""INSERT INTO reports(chat_id,user_id,username,keyword,minutes,start_ts,due_ts,status,end_ts)
                 VALUES(?,?,?,?,?, ?,?,'ongoing',NULL)""",
              (chat_id,user_id,username or "",keyword,minutes,now_ts,due))
    rid=c.lastrowid; conn.commit(); conn.close(); return rid

def finish_report(report_id:int):
    now_ts=int(time.time())
    conn=db_conn(); c=conn.cursor()
    c.execute("UPDATE reports SET status='returned', end_ts=? WHERE id=?", (now_ts, report_id))
    conn.commit(); conn.close()

# ========= 上下班 =========
async def do_checkin(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_message.from_user
    chat_id=update.effective_chat.id

    async with get_lock(chat_id, user.id):
        clean_legacy_open_checkins(chat_id, user.id)

        now = now_local()
        now_ts = int(time.time())
        now_str = fmt_hms(now)
        today = now.strftime("%Y-%m-%d")

        conn=db_conn(); c=conn.cursor()
        # 当天是否已有打卡（无论是否已下班）→ 直接提示“今天已打过卡”
        c.execute("""SELECT start_ts FROM checkins
                     WHERE chat_id=? AND user_id=? AND date=?""",
                  (chat_id, user.id, today))
        row=c.fetchone()
        if row:
            await update.effective_message.reply_text("今天已打过卡，无需重复打卡。")
            conn.close(); return

        # 新建当日打卡
        is_late = fmt_hms(now) > CHECKIN_DEADLINE
        c.execute("""INSERT INTO checkins(chat_id,user_id,username,date,start_ts,end_ts,work_seconds,is_late)
                     VALUES(?,?,?,?,?,NULL,0,?)""",
                  (chat_id, user.id, user.full_name, today, now_ts, int(is_late)))
        conn.commit(); conn.close()

        if is_late:
            total = inc_late_count(chat_id, user.id)  # 累计迟到 +1
            await update.effective_message.reply_text(
                f"❌ 迟到打卡！（时间：{now_str}）\n"
                f"今天要加油哦，调整好心态继续努力 💪\n"
                f"（累计迟到：{total} 次）"
            )
        else:
            await update.effective_message.reply_text(
                f"✅ 打卡成功！（时间：{now_str}）\n"
                "新的一天开始啦，祝你工作顺利，入金不断！加油加油加油 🚀"
            )

async def do_offwork(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_message.from_user
    chat_id=update.effective_chat.id

    async with get_lock(chat_id, user.id):
        clean_legacy_open_checkins(chat_id, user.id)

        now = now_local()
        now_ts = int(time.time())
        today = now.strftime("%Y-%m-%d")

        conn=db_conn(); c=conn.cursor()
        # 找“今天”的打卡记录（当天只允许一条）
        c.execute("""SELECT start_ts FROM checkins
                     WHERE chat_id=? AND user_id=? AND date=?""",
                  (chat_id, user.id, today))
        row=c.fetchone()
        if not row or not row["start_ts"]:
            await update.effective_message.reply_text("今天还未打上班卡，无法下班。")
            conn.close(); return

        start_ts = to_int(row["start_ts"], 0)

        # 报备用时（按实际 end_ts；未归队扣到 min(due, now)）
        c.execute("""SELECT start_ts, due_ts, status, end_ts
                     FROM reports
                     WHERE chat_id=? AND user_id=?
                       AND due_ts > ? AND start_ts < ?""",
                  (chat_id, user.id, start_ts, now_ts))
        report_rows=c.fetchall()
        used = 0
        for r in report_rows:
            rs = to_int(r["start_ts"], 0)
            if r["status"] == "returned" and r["end_ts"] is not None:
                re = to_int(r["end_ts"], now_ts)
            else:
                re = min(to_int(r["due_ts"], now_ts), now_ts)
            if rs > 0:
                used += overlap_seconds(start_ts, now_ts, rs, re)

        gross = max(0, now_ts - start_ts)
        net   = max(0, gross - used)

        # 写回（把今天的记录结算）
        c.execute("""UPDATE checkins SET end_ts=?, work_seconds=?
                     WHERE chat_id=? AND user_id=? AND date=?""",
                  (now_ts, net, chat_id, user.id, today))
        conn.commit(); conn.close()

        await update.effective_message.reply_text(
            "✅ 今日工作已结束 🎉\n"
            f"上班时间：{fmt_dt(start_ts)}\n"
            f"下班时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"总时长：{fmt_duration(gross)}\n"
            f"报备扣除：{fmt_duration(used)}\n"
            f"净工作时长：{fmt_duration(net)}\n\n"
            "辛苦啦！今天的努力不会白费，早点休息，明天继续冲！🌙✨"
        )

# ========= 指令 & 文本入口 =========
async def start_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    total = get_late_count(update.effective_chat.id, u.id)
    await update.effective_message.reply_text(
        "已就绪 ✅\n"
        "上班：发送“上班 / 打卡 / 到岗”\n"
        "下班：发送“下班”\n"
        f"迟到阈值：{CHECKIN_DEADLINE}，‘今天’窗口：{RESET_HOURS} 小时\n"
        "报备：吃饭(30) / wc小(5) / wc大(10) / 抽烟(5) / 厕所(5)\n"
        "归队：1 / 回 / 回来了（按实际用时扣除）\n"
        f"累计迟到：{total} 次"
    )

async def ver_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("ver: final-20250823")

def is_checkin_text(t: str) -> bool:
    keys = ("上班","打卡","到岗")
    return t in {"上班","打卡","到岗"} or any(k in t for k in keys)

async def text_listener(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    chat_id=update.effective_chat.id
    user=update.effective_message.from_user
    t=normalize_text(update.message.text.strip())

    try:
        if is_checkin_text(t):
            await do_checkin(update, context); return
        if ("下班" in t) or (t in OFFWORK_WORDS):
            await do_offwork(update, context); return
        if t in {normalize_text(x) for x in RETURN_WORDS}:
            # 归队：记录实际 end_ts
            async with get_lock(chat_id, user.id):
                row=get_user_ongoing_report(chat_id,user.id)
                if not row:
                    await update.effective_message.reply_text("你当前没有进行中的报备。"); return
                rid,kw,mins,st,due=row
                finish_report(rid)
                used=int(time.time())-to_int(st,0)
                await update.effective_message.reply_text(f"已归队 ✅ 用时：{fmt_duration(used)}")
            return
        # 发起报备（精确 → 包含）
        hit=None
        for k in REPORT_KEYS:
            if t==normalize_text(k): hit=k; break
        if not hit:
            for k in REPORT_KEYS:
                if normalize_text(k) in t: hit=k; break
        if hit:
            async with get_lock(chat_id, user.id):
                cur=get_user_ongoing_report(chat_id,user.id)
                if cur:
                    await update.effective_message.reply_text("你已有进行中的报备，请先回复 1 或“回”结束。"); return
                mins=REPORT_MAP[hit]; create_report(chat_id,user.id,user.full_name,hit,mins)
                await update.effective_message.reply_text(f"已报备：{hit}（{mins} 分钟）。到点请回复 1 或“回”结束。")
            return
    except Exception as e:
        await update.effective_message.reply_text(f"处理消息时出错：{e!s}")

# ========= 启动 =========
async def on_startup(app):
    try: await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception: pass

def main():
    if not BOT_TOKEN:
        print("❌ 请设置 BOT_TOKEN"); return
    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = on_startup
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ver", ver_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_listener))
    print("✅ 机器人已启动"); app.run_polling()

if __name__ == "__main__":
    main()
