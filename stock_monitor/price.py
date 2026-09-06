import FinanceDataReader as fdr
import json
import asyncio
import datetime
import os
from telegram import Bot
from telegram.ext import Application, CommandHandler

# 파일 경로 정의
CONFIG_FILE = 'config_price.json'
MAPPING_FILE = 'mapping_price.json'

def load_json(path):
    if not os.path.exists(path): return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# 글로벌 설정 로드
config = load_json(CONFIG_FILE)
mapping = load_json(MAPPING_FILE)
sent_alerts = set()

# 시총 데이터 가져오기 (Marcap 컬럼 직접 사용)
def get_mcap_str(symbol):
    try:
        df_krx = fdr.StockListing('KRX')
        row = df_krx[df_krx['Code'] == symbol]
        if row.empty: return "정보없음"
        
        mcap_raw = row['Marcap'].values[0]
        mcap_trillion = float(mcap_raw) / 10**12
        return f"{mcap_trillion:.2f}조원"
    except:
        return "계산불가"

# 주가 정보 가져오기
def fetch_stock_info(name):
    if name not in mapping: return None
    code = mapping[name]
    try:
        df = fdr.DataReader(code).tail(2)
        if len(df) < 2: return None
        prev_close = int(df['Close'].iloc[0])
        curr_price = int(df['Close'].iloc[1])
        change_pct = ((curr_price - prev_close) / prev_close) * 100
        return {
            "name": name, "code": code,
            "curr": curr_price, "prev": prev_close,
            "pct": round(change_pct, 2)
        }
    except:
        return None

# 메인 모니터링 루프
async def monitor_loop(context):
    now = datetime.datetime.now()
    
    # 월~금(0~4) 체크 및 시간 체크 (09:03 ~ 15:30)
    is_weekday = now.weekday() < 5
    is_market_time = (now.hour == 9 and now.minute >= 3) or (10 <= now.hour < 15) or (now.hour == 15 and now.minute <= 30)
    
    if not (is_weekday and is_market_time):
        if sent_alerts:
            sent_alerts.clear()
        return

    # 1 & 2번 방: 변동률 알림
    for room_key, room_cfg in config['rooms'].items():
        chat_id = room_cfg['chat_id']
        list_file = room_cfg['list_file']
        
        if os.path.exists(list_file):
            with open(list_file, 'r', encoding='utf-8') as f:
                stocks = [line.strip() for line in f if line.strip()]
            
            for s_name in stocks:
                info = fetch_stock_info(s_name)
                if not info: continue
                
                for threshold in [5, 10, 15, 20, 25]:
                    if abs(info['pct']) >= threshold:
                        direction = "상승 🔴" if info['pct'] > 0 else "하락 🔵"
                        alert_id = f"pct_{room_key}_{s_name}_{direction}_{threshold}"
                        if alert_id not in sent_alerts:
                            mcap = get_mcap_str(info['code'])
                            msg = f"<b>{info['name']}(시총 {mcap})</b>\n{threshold}% {direction}\n{info['prev']:,}원 -> {info['curr']:,}원 ({info['pct']}%)"
                            await context.bot.send_message(chat_id, msg, parse_mode='HTML')
                            sent_alerts.add(alert_id)

    # 3번 방: 목표주가 알림
    target_file = config['target_file']
    target_chat_id = config['target_room_id']
    if os.path.exists(target_file):
        targets = load_json(target_file)
        for s_name, t_info in targets.items():
            info = fetch_stock_info(s_name)
            if not info: continue
            
            goal, cond = t_info['price'], t_info['cond']
            alert_id = f"target_{s_name}_{goal}_{cond}"
            is_triggered = (cond == "이상" and info['curr'] >= goal) or (cond == "이하" and info['curr'] <= goal)
            
            if is_triggered and alert_id not in sent_alerts:
                mcap = get_mcap_str(info['code'])
                msg = f"<b>{info['name']}(시총 {mcap})</b>\n목표주가 {goal:,}원 {cond} 달성!\n현재가: {info['curr']:,}원"
                await context.bot.send_message(target_chat_id, msg, parse_mode='HTML')
                sent_alerts.add(alert_id)

# 명령어 처리: 추가
async def add_list(update, context):
    if update.effective_user.id != config['admin_id']: return
    try:
        cmd = update.message.text.split()[0]
        room = "chemhold" if "1" in cmd else "jm"
        name = context.args[0]
        if name not in mapping:
            await update.message.reply_text(f"'{name}' 종목을 찾을 수 없습니다.")
            return
        
        file_path = config['rooms'][room]['list_file']
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write(f"{name}\n")
        await update.message.reply_text(f"[{room.upper()}] {name} 추가 완료.")
    except: await update.message.reply_text("사용법: /add1 종목명")

# 명령어 처리: 삭제
async def del_list(update, context):
    if update.effective_user.id != config['admin_id']: return
    try:
        cmd = update.message.text.split()[0]
        room = "chemhold" if "1" in cmd else "jm"
        name = context.args[0]
        file_path = config['rooms'][room]['list_file']
        
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            with open(file_path, 'w', encoding='utf-8') as f:
                for line in lines:
                    if line.strip() != name:
                        f.write(line)
            await update.message.reply_text(f"[{room.upper()}] {name} 삭제 완료.")
    except: await update.message.reply_text("사용법: /del1 종목명")

async def add_target(update, context):
    if update.effective_user.id != config['admin_id']: return
    try:
        name, price, cond = context.args[0], int(context.args[1]), context.args[2]
        data = load_json(config['target_file'])
        data[name] = {"price": price, "cond": cond}
        save_json(config['target_file'], data)
        await update.message.reply_text(f"[TARGET] {name} 설정 완료.")
    except: await update.message.reply_text("사용법: /target 종목명 가격 이상/이하")

async def del_target(update, context):
    if update.effective_user.id != config['admin_id']: return
    try:
        name = context.args[0]
        data = load_json(config['target_file'])
        if name in data:
            del data[name]
            save_json(config['target_file'], data)
            await update.message.reply_text(f"[TARGET] {name} 삭제 완료.")
    except: await update.message.reply_text("사용법: /deltarget 종목명")

if __name__ == '__main__':
    app = Application.builder().token(config['token']).build()
    
    app.add_handler(CommandHandler("add1", add_list))
    app.add_handler(CommandHandler("add2", add_list))
    app.add_handler(CommandHandler("del1", del_list))
    app.add_handler(CommandHandler("del2", del_list))
    app.add_handler(CommandHandler("target", add_target))
    app.add_handler(CommandHandler("deltarget", del_target))
    
    if app.job_queue:
        app.job_queue.run_repeating(monitor_loop, interval=30, first=10)
    
    app.run_polling()