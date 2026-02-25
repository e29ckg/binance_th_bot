# config.py

# --- Trading Logic ---
TIMEFRAME = 15          # นาทีกราฟ (1, 5, 15, 60, 240, 1440)
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

# --- Money Management ---
TAKE_PROFIT_PCT = 1.5   # % กำไรที่จะขาย (เช่น 1.5%)
DCA_DROP_PCT = 2.0      # % ราคาตกที่จะซื้อเพิ่ม (DCA)
FEE_BUFFER = 0.52       # % เผื่อค่าธรรมเนียม (0.25+0.25 + vat นิดหน่อย)

# --- Trailing Take Profit (TTP) ---
TTP_ACTIVATION_PCT = 1.5  # กำไรกี่เปอร์เซ็นต์ถึงจะ "เปิดโหมด" วิ่งตามดอย (เช่น 1.5%)
TTP_DROP_PCT = 0.5        # ถ้าราคาตกลงมาจาก "จุดสูงสุด" กี่เปอร์เซ็นต์ ถึงจะกดขาย (เช่น 0.5%)

# --- System ---
DB_NAME = "bitkub_bot.db"
