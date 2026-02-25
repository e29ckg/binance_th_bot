import asyncio
import aiosqlite
import pandas as pd
import pandas_ta as ta
from typing import Callable, Optional

class BotEngine:
    def __init__(self, client, db_name: str, broadcast_func: Callable):
        self.client = client
        self.db_name = db_name
        self.broadcast = broadcast_func
        self.is_running = False
        
        # ตั้งค่าการเทรด (ปรับแต่งได้ตามต้องการ)
        self.symbols = ["BTCUSDT", "ETHUSDT"] # ใช้ Format ของ Binance
        self.trade_amount_usdt = 15.0  # ยอดซื้อต่อไม้ (ต้อง > 10 USDT ของ Binance)
        
        # ตั้งค่า Money Management
        self.dca_drop_pct = 0.05       # DCA เมื่อราคาตก 5%
        self.ttp_activation_pct = 0.03 # เริ่มทำ Trailing เมื่อกำไร 3%
        self.ttp_trail_pct = 0.01      # ขายเมื่อราคาตกลงจากจุดสูงสุด 1%
        
        # ตัวแปรสำหรับเก็บสถานะ Trailing Take Profit แบบ In-memory
        self.peak_prices = {} # { "BTCUSDT": 65000.0 }

    async def log(self, message: str, level: str = "info"):
        """ส่ง Log ไปแสดงที่ Dashboard แบบ Real-time"""
        print(f"[{level.upper()}] {message}")
        if self.broadcast:
            await self.broadcast({"type": level, "msg": message})

    def stop(self):
        self.is_running = False

    async def run(self):
        """Main Loop ของบอท ทำงานวนไปเรื่อยๆ แบบ Async"""
        self.is_running = True
        await self.log("Bot Engine started successfully.")
        
        while self.is_running:
            for symbol in self.symbols:
                try:
                    # 1. ดึงข้อมูลกราฟและแปลงเป็น DataFrame
                    candles = await self.client.get_candles(symbol, interval="15m", limit=100)
                    df = pd.DataFrame(candles)
                    
                    # 2. ให้ AI วิเคราะห์สภาวะตลาด (Strategy 4) และเลือกกลยุทธ์
                    current_price = df['close'].iloc[-1]
                    regime, active_strategy = await self.strategy_4_auto_ai(df)
                    
                    # 3. จัดการออเดอร์ค้าง (DCA & Trailing Take Profit)
                    await self.manage_open_positions(symbol, current_price)
                    
                    # 4. หาจังหวะเข้าซื้อ (ถ้ายังไม่มีไม้ที่เปิดอยู่)
                    has_open_position = await self.check_open_position(symbol)
                    if not has_open_position:
                        signal = active_strategy(df)
                        if signal == "BUY":
                            await self.execute_trade(symbol, "BUY", current_price, "Strategy_Auto")
                            
                except Exception as e:
                    await self.log(f"Error processing {symbol}: {str(e)}", "error")
            
            # หน่วงเวลาเพื่อป้องกัน Rate Limit ของ Binance
            await asyncio.sleep(10)

    # ==========================================
    # ส่วนที่ 1: กลยุทธ์การเทรด (Multi-Strategy)
    # ==========================================
    def strategy_1_trend_reversal(self, df: pd.DataFrame) -> str:
        """Strategy 1: ปลอดภัย ซื้อเมื่อ Oversold + ขาลง"""
        df.ta.rsi(length=14, append=True)
        rsi = df['RSI_14'].iloc[-1]
        if rsi < 30: return "BUY"
        if rsi > 70: return "SELL"
        return "HOLD"

    def strategy_2_rsi_scalping(self, df: pd.DataFrame) -> str:
        """Strategy 2: เล่นรอบสั้น"""
        df.ta.rsi(length=7, append=True) # ใช้ RSI สั้นลง
        rsi = df['RSI_7'].iloc[-1]
        if rsi < 25: return "BUY"
        if rsi > 75: return "SELL"
        return "HOLD"

    def strategy_3_macd_cross(self, df: pd.DataFrame) -> str:
        """Strategy 3: ถือรันเทรนด์"""
        macd = df.ta.macd(fast=12, slow=26, signal=9)
        macd_line = macd['MACD_12_26_9'].iloc[-1]
        signal_line = macd['MACDs_12_26_9'].iloc[-1]
        
        # ตัดขึ้นซื้อ ตัดลงขาย
        if macd_line > signal_line and macd['MACD_12_26_9'].iloc[-2] <= macd['MACDs_12_26_9'].iloc[-2]:
            return "BUY"
        return "HOLD"

    async def strategy_4_auto_ai(self, df: pd.DataFrame):
        """
        Strategy 4 (Market Regime Detection)
        วิเคราะห์ ADX และ EMA เพื่อหาสภาวะตลาดและเลือกกลยุทธ์ 1-3
        """
        df.ta.adx(length=14, append=True)
        df.ta.ema(length=50, append=True)
        
        adx = df['ADX_14'].iloc[-1]
        close = df['close'].iloc[-1]
        ema50 = df['EMA_50'].iloc[-1]
        
        # เช็คความแรงของเทรนด์
        if adx > 25:
            if close > ema50:
                regime = "BULLISH"
                strategy = self.strategy_3_macd_cross # เทรนด์ขาขึ้นชัดเจน ใช้ MACD รันเทรนด์
            else:
                regime = "BEARISH"
                strategy = self.strategy_1_trend_reversal # เทรนด์ขาลง ใช้ Reversal หาจุดกลับตัว
        else:
            regime = "SIDEWAYS"
            strategy = self.strategy_2_rsi_scalping # ไม่มีเทรนด์ เล่นสั้น Scalping
            
        await self.log(f"Market Regime: {regime} | Selected Strategy: {strategy.__name__}")
        return regime, strategy

    # ==========================================
    # ส่วนที่ 2: Money Management (DCA & TTP)
    # ==========================================
    async def manage_open_positions(self, symbol: str, current_price: float):
        """ตรวจสอบและจัดการ Smart DCA และ Trailing Take Profit"""
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM trades WHERE symbol = ? AND status = 'OPEN'", (symbol,))
            trades = await cursor.fetchall()
            
            if not trades:
                self.peak_prices.pop(symbol, None) # เคลียร์ค่า Peak ถ้าไม่มีออเดอร์
                return
                
            # คำนวณต้นทุนเฉลี่ย
            total_amount = sum(t['amount'] for t in trades)
            total_cost = sum(t['price'] * t['amount'] for t in trades)
            avg_price = total_cost / total_amount if total_amount > 0 else 0
            
            profit_pct = (current_price - avg_price) / avg_price

            # 1. เช็ค Smart DCA (ถ้าราคาตกเกินกำหนด)
            if profit_pct <= -self.dca_drop_pct:
                await self.log(f"[{symbol}] ราคาตก {profit_pct*100:.2f}% ทำการ Smart DCA!")
                await self.execute_trade(symbol, "BUY", current_price, "DCA")
                return # ออกจากฟังก์ชัน รอให้ลูปรอบหน้ามาจัดการต่อ

            # 2. เช็ค Trailing Take Profit (TTP)
            if profit_pct >= self.ttp_activation_pct:
                # อัปเดตจุดสูงสุด
                current_peak = self.peak_prices.get(symbol, avg_price)
                if current_price > current_peak:
                    self.peak_prices[symbol] = current_price
                    await self.log(f"[{symbol}] อัปเดตจุด Trailing Peak ใหม่: {current_price}")
                
                # เช็คว่าราคาตกลงมาจากจุดสูงสุดเกินระยะ Trailing หรือยัง
                drawdown_from_peak = (self.peak_prices[symbol] - current_price) / self.peak_prices[symbol]
                if drawdown_from_peak >= self.ttp_trail_pct:
                    await self.log(f"[{symbol}] ราคาตกลงจากจุดสูงสุด ทริกเกอร์ Trailing Take Profit! รวบยอดขายทั้งหมด")
                    await self.execute_trade(symbol, "SELL", current_price, "TTP", close_all_amount=total_amount)

    # ==========================================
    # ส่วนที่ 3: Execution & Database
    # ==========================================
    async def check_open_position(self, symbol: str) -> bool:
        """เช็คว่ามีไม้ค้างอยู่หรือไม่ (เพื่อป้องกันการซื้อซ้ำซ้อนถ้าไม่ได้เข้าเงื่อนไข DCA)"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM trades WHERE symbol = ? AND status = 'OPEN'", (symbol,))
            result = await cursor.fetchone()
            return result[0] > 0

    async def execute_trade(self, symbol: str, side: str, price: float, strategy_name: str, close_all_amount: float = None):
        """ส่งคำสั่งซื้อขายไปที่ Binance และบันทึกลงฐานข้อมูล"""
        
        # คำนวณจำนวนเหรียญ (ถ้าเป็นการขาย TTP ให้ขายจำนวนทั้งหมดที่ระบุ)
        if side == "SELL" and close_all_amount is not None:
            qty = close_all_amount
        else:
            qty = self.trade_amount_usdt / price
        
        # ⚠️ กฎ Binance: มูลค่าต้องเกิน Min Notional (เช่น 10 USDT)
        notional_value = qty * price
        if notional_value < 10.0:
            await self.log(f"ปฏิเสธคำสั่ง {side} {symbol}: มูลค่า ({notional_value:.2f} USDT) ต่ำกว่าขั้นต่ำของ Binance (10 USDT)", "warning")
            return

        try:
            # เรียก API ของ Binance ที่เราเขียนเตรียมไว้
            order_res = await self.client.place_order(symbol, amount=qty, side=side, order_type="MARKET")
            
            # Binance จะคืน orderId กลับมา (เป็นตัวเลขยาวๆ)
            order_id = str(order_res.get('orderId', 'TEST_ORDER_ID'))
            executed_qty = float(order_res.get('executedQty', qty))
            
            async with aiosqlite.connect(self.db_name) as db:
                if side == "BUY":
                    # บันทึกไม้ซื้อใหม่
                    await db.execute(
                        "INSERT INTO trades (symbol, order_id, side, price, amount, strategy, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (symbol, order_id, side, price, executed_qty, strategy_name, 'OPEN')
                    )
                elif side == "SELL":
                    # ปิดไม้ซื้อทั้งหมดที่เป็น OPEN อยู่
                    await db.execute(
                        "UPDATE trades SET status = 'CLOSED' WHERE symbol = ? AND status = 'OPEN'", 
                        (symbol,)
                    )
                await db.commit()
                
            await self.log(f"ดำเนินการ {side} {symbol} สำเร็จ! [Strategy: {strategy_name}, Price: {price}, Qty: {executed_qty}]", "success")
            
        except Exception as e:
            await self.log(f"เกิดข้อผิดพลาดในการส่งออเดอร์ {symbol}: {str(e)}", "error")

    def set_trade_amount(self, new_amount: float) -> tuple[bool, str]:
        """
        อัปเดตยอดการเข้าซื้อต่อไม้ (USDT)
        คืนค่า (True/False, ข้อความแจ้งเตือน)
        """
        if new_amount < 10.0:
            return False, f"ล้มเหลว: ยอดซื้อ {new_amount} USDT ต่ำกว่าขั้นต่ำของ Binance (10 USDT)"
        
        self.trade_amount_usdt = new_amount
        return True, f"อัปเดตยอดซื้อต่อไม้เป็น {self.trade_amount_usdt} USDT เรียบร้อยแล้ว!"