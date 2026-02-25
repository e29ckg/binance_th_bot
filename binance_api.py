import hmac
import hashlib
import time
import httpx
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List

class BinanceAsyncClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        """
        เริ่มต้นเชื่อมต่อ API ของ Binance
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://testnet.binance.vision" if testnet else "https://api.binance.com"
        self.symbol_filters = {} # เก็บข้อมูลกฎของแต่ละเหรียญ (Lot Size, Min Notional)

    async def _get_signature(self, query_string: str) -> str:
        """สร้าง HMAC SHA256 Signature สำหรับ Private Endpoints"""
        return hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    async def _request(self, method: str, endpoint: str, params: Optional[Dict] = None, signed: bool = False):
        """จัดการการส่ง Request ทั้งแบบ Public และ Private"""
        if params is None:
            params = {}
            
        headers = {"X-MBX-APIKEY": self.api_key}
        
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            # สร้าง Query String เพื่อไปทำ Signature
            query_string = "&".join([f"{k}={v}" for k, v in params.items()])
            params['signature'] = await self._get_signature(query_string)

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method=method,
                url=f"{self.base_url}{endpoint}",
                params=params,
                headers=headers,
                timeout=10.0
            )
            response.raise_for_status() # แจ้งเตือนถ้า API ตอบกลับเป็น Error
            return response.json()

    # ==========================================
    # ฟังก์ชันจัดการทศนิยม (แก้ปัญหา Scientific Notation)
    # ==========================================
    async def load_exchange_info(self):
        """ดึงข้อมูลกฎของกระดานเทรด (ต้องเรียกใช้ตอนบอทเริ่มทำงาน)"""
        data = await self._request("GET", "/api/v3/exchangeInfo")
        for symbol_data in data['symbols']:
            self.symbol_filters[symbol_data['symbol']] = {
                filter['filterType']: filter for filter in symbol_data['filters']
            }

    def format_number(self, symbol: str, value: float, filter_type: str) -> str:
        """
        แปลงตัวเลขเป็น String ที่ไม่มี Scientific Notation และปัดเศษทศนิยมลงให้ตรงกับกฎ
        - filter_type='LOT_SIZE' สำหรับ Quantity (จำนวนเหรียญ)
        - filter_type='PRICE_FILTER' สำหรับ Price (ราคา)
        """
        if symbol not in self.symbol_filters:
            # ถ้าไม่มีข้อมูล ให้คืนค่าเป็น string ธรรมดาไปก่อน
            return f"{Decimal(str(value)):f}"

        filters = self.symbol_filters[symbol].get(filter_type, {})
        step_size_str = filters.get('stepSize') or filters.get('tickSize')
        
        if not step_size_str:
            return f"{Decimal(str(value)):f}"

        # แปลงเป็น Decimal เพื่อการคำนวณที่แม่นยำ
        val_dec = Decimal(str(value))
        step_dec = Decimal(step_size_str).normalize()
        
        # ปัดเศษลง (ROUND_DOWN) ตาม step size ป้องกันปัญหา Insufficient Balance
        quantized_val = (val_dec // step_dec) * step_dec
        
        # คืนค่าเป็น String (เช่น '0.00045') จะไม่มี e-05 โผล่มาแน่นอน
        return f"{quantized_val:f}"

    # ==========================================
    # Interface ที่ BotEngine ของคุณต้องการ
    # ==========================================
    async def get_server_status(self) -> bool:
        """เช็คสถานะ API"""
        try:
            res = await self._request("GET", "/api/v3/ping")
            return res == {}
        except:
            return False

    async def get_candles(self, symbol: str, interval: str = "1h", limit: int = 100) -> List[Dict]:
        """ดึงกราฟแท่งเทียน"""
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        data = await self._request("GET", "/api/v3/klines", params=params)
        
        # จัด Format ให้ตรงกับที่ BotEngine ใช้งาน
        formatted_candles = []
        for candle in data:
            formatted_candles.append({
                "time": candle[0],
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5])
            })
        return formatted_candles

    async def get_wallet(self) -> Dict[str, float]:
        """เช็คยอดเงินที่เหลือ (คืนค่าเฉพาะเหรียญที่มีจำนวนมากกว่า 0)"""
        data = await self._request("GET", "/api/v3/account", signed=True)
        balances = {}
        for b in data['balances']:
            free_amt = float(b['free'])
            if free_amt > 0:
                balances[b['asset']] = free_amt
        return balances

    async def place_order(self, symbol: str, amount: float, rate: float = 0, side: str = "BUY", order_type: str = "MARKET"):
        """ส่งคำสั่งซื้อขาย พร้อมจัดการเรื่องทศนิยมให้อัตโนมัติ"""
        # เตรียมปริมาณเหรียญ (Quantity)
        formatted_qty = self.format_number(symbol, amount, 'LOT_SIZE')
        
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": formatted_qty
        }

        # ถ้าเป็น LIMIT order ต้องใส่ราคา (Price) และ Time in Force
        if order_type.upper() == "LIMIT":
            formatted_price = self.format_number(symbol, rate, 'PRICE_FILTER')
            params["price"] = formatted_price
            params["timeInForce"] = "GTC" # Good Till Cancel

        return await self._request("POST", "/api/v3/order", params=params, signed=True)

    async def get_open_orders(self, symbol: str):
        """ดึงออเดอร์ที่ตั้งรอไว้และยังไม่แมตช์"""
        params = {"symbol": symbol}
        return await self._request("GET", "/api/v3/openOrders", params=params, signed=True)

    async def cancel_order(self, symbol: str, order_id: int):
        """ยกเลิกออเดอร์ (Binance ไม่สน side ในการยกเลิก ใช้แค่ order_id)"""
        params = {"symbol": symbol, "orderId": order_id}
        return await self._request("DELETE", "/api/v3/order", params=params, signed=True)