import os # เพิ่ม import os
import asyncio
import aiosqlite
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import List
from dotenv import load_dotenv # เพิ่ม import โหลด .env

# นำเข้าคลาสที่เราสร้างไว้
from binance_api import BinanceAsyncClient
from bot_engine import BotEngine 

# ==========================================
# 1. โหลด Environment Variables
# ==========================================
load_dotenv() # คำสั่งนี้จะไปอ่านไฟล์ .env มาใส่ในระบบ

# ดึงค่าจาก .env (ถ้าไม่มีจะใช้ค่า Default ที่เรากำหนดไว้ด้านหลัง)
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
DB_NAME = os.getenv("DB_NAME", "crypto_bot.db")

# แปลงค่า USE_TESTNET จากตัวอักษรเป็น Boolean
USE_TESTNET_STR = os.getenv("USE_TESTNET", "True").lower()
IS_TESTNET = USE_TESTNET_STR in ("true", "1", "yes")

# ตรวจสอบความปลอดภัยเบื้องต้น
if not API_KEY or not API_SECRET:
    raise ValueError("🚨 ERROR: ไม่พบ API_KEY หรือ API_SECRET ในไฟล์ .env!")

app = FastAPI(title="Binance Auto Crypto Bot")



# อนุญาตให้ Frontend (Dashboard) เข้าถึง API ได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ใช้ testnet=True เพื่อทดสอบด้วยเงินจำลองก่อน (ถ้าใช้เงินจริงเปลี่ยนเป็น False)
binance_client = BinanceAsyncClient(
    api_key=API_KEY, 
    api_secret=API_SECRET, 
    testnet=IS_TESTNET
)
bot_engine = None
active_websockets: List[WebSocket] = []

# ==========================================
# 2. ฟังก์ชันจัดการ Database (SQLite โหมด WAL)
# ==========================================
async def init_db():
    """สร้างตารางและเปิดโหมด WAL (Write-Ahead Logging) เพื่อให้รองรับ Async ได้ดีขึ้น"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('PRAGMA journal_mode=WAL;')
        # สังเกตว่าเราใช้ order_id เป็น TEXT เพื่อรองรับตัวเลขยาวๆ ของ Binance
        await db.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                order_id TEXT, 
                side TEXT,
                price REAL,
                amount REAL,
                strategy TEXT,
                status TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()
    print("Database initialized with WAL mode.")

# ==========================================
# 3. ฟังก์ชัน Broadcast ส่งข้อมูลไป Dashboard แบบ Real-time
# ==========================================
async def broadcast_log(log_data: dict):
    """ส่งข้อความไปยังทุก WebSocket ที่เชื่อมต่ออยู่ (Dashboard)"""
    for connection in active_websockets:
        try:
            await connection.send_json(log_data)
        except Exception as e:
            print(f"WebSocket send error: {e}")

# ==========================================
# 4. FastAPI Events (Startup & Shutdown)
# ==========================================
@app.on_event("startup")
async def startup_event():
    global bot_engine
    
    # 1. เตรียม Database
    await init_db()
    
    # 2. โหลดกฎของกระดานเทรด (สำคัญมากสำหรับจัดการทศนิยม Binance)
    print("Loading exchange info from Binance...")
    await binance_client.load_exchange_info()
    
    # 3. เริ่มต้น BotEngine และส่ง Client, DB, และฟังก์ชัน Broadcast เข้าไป
    bot_engine = BotEngine(
        client=binance_client, 
        db_name=DB_NAME, 
        broadcast_func=broadcast_log
    )
    
    # 4. รันระบบเทรดเป็น Background Task (เพื่อไม่ให้บล็อก API)
    asyncio.create_task(bot_engine.run())
    print("Bot Engine is running in the background!")

@app.on_event("shutdown")
async def shutdown_event():
    if bot_engine:
        bot_engine.stop()
    print("Bot gracefully shut down.")

# ==========================================
# 5. API Endpoints (สำหรับ Dashboard ดึงข้อมูล)
# ==========================================
@app.get("/api/status")
async def get_bot_status():
    """ให้ Dashboard ยิงมาถามสถานะบอทและยอดเงิน"""
    server_ok = await binance_client.get_server_status()
    wallet = await binance_client.get_wallet()
    
    return {
        "status": "running" if bot_engine.is_running else "stopped",
        "binance_api_connected": server_ok,
        "wallet_balances": wallet,
        "current_strategies": bot_engine.active_strategies
    }

@app.get("/api/trades")
async def get_trade_history(limit: int = 50):
    """ดึงประวัติการเทรด 50 รายการล่าสุดจาก Database"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", 
            (limit,)
        )
        rows = await cursor.fetchall()
        # แปลงข้อมูลจาก Row ให้เป็น List of Dictionaries เพื่อส่งเป็น JSON
        return [dict(row) for row in rows]

# ==========================================
# 6. WebSocket Endpoint (สำหรับ Dashboard รับ Logs)
# ==========================================
@app.websocket("/ws/dashboard")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        while True:
            # รอรับข้อมูล JSON จาก Dashboard
            data = await websocket.receive_json()
            command = data.get("command")
            
            if command == "stop":
                bot_engine.stop()
                await broadcast_log({"type": "warning", "msg": "Bot stopped by user."})
                
            elif command == "start":
                if not bot_engine.is_running:
                    asyncio.create_task(bot_engine.run())
                    await broadcast_log({"type": "success", "msg": "Bot started by user."})
                    
            # 🟢 ส่วนที่เพิ่มเข้ามาใหม่สำหรับการอัปเดตยอดเงิน
            elif command == "update_trade_amount":
                try:
                    new_amount = float(data.get("value", 0))
                    success, msg = bot_engine.set_trade_amount(new_amount)
                    
                    # ส่งผลลัพธ์กลับไปแจ้งเตือนที่หน้า Dashboard
                    log_type = "success" if success else "error"
                    await broadcast_log({"type": log_type, "msg": msg})
                except ValueError:
                    await broadcast_log({"type": "error", "msg": "รูปแบบตัวเลขไม่ถูกต้อง"})
                
    except WebSocketDisconnect:
        active_websockets.remove(websocket)