import os
import time
import smtplib
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header

# ================= 1. 基础配置 =================
# 必盯的 10 个自选标的（美股合约 + 重点币），剩下 40 个自动按 Gate 成交额补齐
CUSTOM_SYMBOLS = [
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "ZEC_USDT", "HYPE_USDT",
    "SNDK_USDT", "SPCX_USDT", "MU_USDT", "NVDA_USDT", "TSLA_USDT"
]
TOTAL_MONITOR_COUNT = 50

QQ_EMAIL = os.getenv("QQ_EMAIL", "")
QQ_AUTH_CODE = os.getenv("QQ_AUTH_CODE", "")
GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"


# ================= 2. 数据获取 =================
def get_50_symbols():
    symbols = list(CUSTOM_SYMBOLS)
    try:
        resp = requests.get(f"{GATE_BASE}/tickers", timeout=10)
        resp.raise_for_status()
        tickers = resp.json()
        tickers.sort(key=lambda x: float(x.get("volume_24h_settle", 0)), reverse=True)
        for t in tickers:
            c = t["contract"]
            if c not in symbols:
                symbols.append(c)
            if len(symbols) >= TOTAL_MONITOR_COUNT:
                break
    except Exception as e:
        print(f"[获取热门标的失败，仅监控自选] {e}")
    return symbols[:TOTAL_MONITOR_COUNT]


def fetch_candles(symbol: str, interval: str = "15m", limit: int = 24):
    url = f"{GATE_BASE}/candlesticks"
    resp = requests.get(url, params={"contract": symbol, "interval": interval, "limit": limit}, timeout=8)
    resp.raise_for_status()
    return resp.json()


def fetch_funding(symbol: str) -> float:
    url = f"{GATE_BASE}/contracts/{symbol}"
    resp = requests.get(url, timeout=8)
    resp.raise_for_status()
    return float(resp.json().get("funding_rate", 0))


# ================= 3. QQ 邮箱推送 =================
def send_email(title: str, content: str):
    print(f"\n🚨 {title}\n{content}\n" + "=" * 45)
    if not QQ_EMAIL or not QQ_AUTH_CODE:
        print("[未配置邮箱或授权码，跳过发送]")
        return
    try:
        msg = MIMEText(content, "plain", "utf-8")
        msg["From"] = QQ_EMAIL
        msg["To"] = QQ_EMAIL
        msg["Subject"] = Header(f"🚨 {title}", "utf-8")

        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=15) as server:
            server.login(QQ_EMAIL, QQ_AUTH_CODE)
            server.sendmail(QQ_EMAIL, [QQ_EMAIL], msg.as_string())
        print("[QQ邮箱推送成功]")
    except Exception as e:
        print(f"[QQ邮箱推送失败] {e}")


# ================= 4. 核心扫描逻辑 =================
def scan_once():
    symbols = get_50_symbols()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始巡检 {len(symbols)} 个标的...")
    hit_count = 0

    for sym in symbols:
        try:
            candles = fetch_candles(sym, "15m", limit=24)
            if len(candles) < 22:
                continue

            closed = candles[-2]      # 刚收盘的 15m K 线
            history = candles[-22:-2] # 前 20 根（5 小时）高低点

            o, h, l, c = float(closed["o"]), float(closed["h"]), float(closed["l"]), float(closed["c"])
            total_range = h - l
            if total_range <= 0:
                continue

            prev_high = max(float(x["h"]) for x in history)
            prev_low = min(float(x["l"]) for x in history)
            body = abs(c - o)
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l

            signal_type = None
            # 向下扫穿前低 + 长下影线收回（潜在右侧多）
            if l < prev_low and c > prev_low and (lower_wick / total_range >= 0.45) and (lower_wick >= body * 1.2):
                signal_type = "15m 向下扫低收回（潜在多）"
            # 向上刺破前高 + 长上影线收回（潜在右侧空）
            elif h > prev_high and c < prev_high and (upper_wick / total_range >= 0.45) and (upper_wick >= body * 1.2):
                signal_type = "15m 向上扫高收回（潜在空）"

            if signal_type:
                hit_count += 1
                c_1h = fetch_candles(sym, "1h", limit=12)
                h1_high = max(float(x["h"]) for x in c_1h[:-1])
                h1_low = min(float(x["l"]) for x in c_1h[:-1])
                funding = fetch_funding(sym)

                content = (
                    f"标的：{sym}\n"
                    f"信号：{signal_type}\n"
                    f"15m刚收盘：开={o} | 高={h} | 低={l} | 收={c}\n"
                    f"前20根15m区间：低={prev_low} ~ 高={prev_high}\n"
                    f"近12小时1H区间：低={h1_low} ~ 高={h1_high}\n"
                    f"当前资金费率：{funding * 100:.4f}%\n\n"
                    f"提醒：打开盘面看 1H 大趋势与大户多空比，符合再动手，严守硬止损。"
                )
                send_email(f"{sym} {signal_type}", content)

            time.sleep(0.15)
        except Exception as e:
            print(f"[{sym}] 跳过: {e}")

    print(f"本轮巡检结束，共触发 {hit_count} 个信号。")


if __name__ == "__main__":
    scan_once()
