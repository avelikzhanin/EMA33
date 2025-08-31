import asyncio
import os
import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
import aiohttp
import json
from dataclasses import dataclass
from enum import Enum

# Telegram Bot
import telegram
from telegram import Bot

# Tinkoff Invest API
from tinkoff.invest import Client, RequestError, MarketDataRequest, GetCandlesRequest
from tinkoff.invest.schemas import CandleInterval, Instrument
from tinkoff.invest.utils import now

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class Signal:
    """Класс для хранения торгового сигнала"""
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    signal_time: datetime
    setup_description: str
    risk_reward_1: float
    risk_reward_2: float
    risk_reward_3: float

class SignalStatus(Enum):
    WAITING = "waiting"
    TRIGGERED = "triggered"
    CLOSED = "closed"

# Топ-10 акций Мосбиржи (тикеры для Tinkoff API)
TOP_MOEX_STOCKS = [
    "SBER",    # Сбербанк
    "GAZP",    # Газпром
    "LKOH",    # ЛУКОЙЛ
    "YNDX",    # Яндекс
    "GMKN",    # ГМК Норильский никель
    "NVTK",    # Новатэк
    "ROSN",    # Роснефть
    "MTSS",    # МТС
    "MGNT",    # Магнит
    "PLZL"     # Полюс
]

class TradingBot:
    def __init__(self):
        self.tinkoff_token = os.getenv('TINKOFF_TOKEN')
        self.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        self.bot = Bot(token=self.telegram_token)
        self.active_signals: Dict[str, Signal] = {}
        self.instruments_cache: Dict[str, str] = {}  # ticker -> figi
        
    async def initialize(self):
        """Инициализация бота и кэширование инструментов"""
        try:
            async with Client(self.tinkoff_token) as client:
                # Получаем информацию об инструментах
                instruments = await client.instruments.shares()
                for instrument in instruments.instruments:
                    if instrument.ticker in TOP_MOEX_STOCKS:
                        self.instruments_cache[instrument.ticker] = instrument.figi
                        
            logger.info(f"Инициализация завершена. Найдено инструментов: {len(self.instruments_cache)}")
            await self.send_telegram_message("🤖 Торговый бот запущен и готов к работе!")
            
        except Exception as e:
            logger.error(f"Ошибка инициализации: {e}")
            await self.send_telegram_message(f"❌ Ошибка инициализации: {str(e)}")

    async def get_candles(self, figi: str, interval: CandleInterval, days: int = 2) -> pd.DataFrame:
        """Получение свечных данных"""
        try:
            async with Client(self.tinkoff_token) as client:
                from_time = now() - timedelta(days=days)
                to_time = now()
                
                request = GetCandlesRequest(
                    figi=figi,
                    from_=from_time,
                    to=to_time,
                    interval=interval
                )
                
                candles = await client.market_data.get_candles(request=request)
                
                data = []
                for candle in candles.candles:
                    data.append({
                        'time': candle.time,
                        'open': float(candle.open.units + candle.open.nano / 1e9),
                        'high': float(candle.high.units + candle.high.nano / 1e9),
                        'low': float(candle.low.units + candle.low.nano / 1e9),
                        'close': float(candle.close.units + candle.close.nano / 1e9),
                        'volume': candle.volume
                    })
                
                df = pd.DataFrame(data)
                if not df.empty:
                    df['time'] = pd.to_datetime(df['time'])
                    df = df.set_index('time')
                    
                return df
                
        except Exception as e:
            logger.error(f"Ошибка получения данных для {figi}: {e}")
            return pd.DataFrame()

    def calculate_ema(self, prices: pd.Series, period: int) -> pd.Series:
        """Расчет экспоненциальной скользящей средней"""
        return prices.ewm(span=period).mean()

    def detect_support_level(self, df: pd.DataFrame, lookback: int = 20) -> float:
        """Определение уровня поддержки"""
        if len(df) < lookback:
            return df['low'].min()
        
        recent_lows = df['low'].tail(lookback)
        return recent_lows.min()

    def check_ema_breakout(self, df: pd.DataFrame, ema_period: int = 33) -> bool:
        """Проверка пробоя EMA вверх"""
        if len(df) < ema_period + 5:
            return False
            
        df['ema33'] = self.calculate_ema(df['close'], ema_period)
        
        # Проверяем последние 3-5 свечей на пробой
        for i in range(-5, 0):
            if (df.iloc[i-1]['close'] <= df.iloc[i-1]['ema33'] and 
                df.iloc[i]['close'] > df.iloc[i]['ema33']):
                return True
        return False

    def check_retest_ema(self, df: pd.DataFrame, ema_period: int = 33) -> bool:
        """Проверка ретеста EMA33"""
        if len(df) < ema_period + 10:
            return False
            
        df['ema33'] = self.calculate_ema(df['close'], ema_period)
        
        # Ищем касание или приближение к EMA33 после пробоя
        recent_candles = df.tail(5)
        for _, candle in recent_candles.iterrows():
            if abs(candle['low'] - candle['ema33']) / candle['ema33'] < 0.005:  # В пределах 0.5%
                return True
        return False

    def analyze_setup(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        """Анализ сетапа для конкретного инструмента"""
        try:
            if len(df) < 50:
                return None
                
            # Расчет EMA33
            df['ema33'] = self.calculate_ema(df['close'], 33)
            
            # Проверяем наличие отскока от поддержки
            support_level = self.detect_support_level(df, 20)
            
            # Проверяем пробой EMA33
            ema_breakout = self.check_ema_breakout(df)
            
            # Проверяем ретест EMA33
            ema_retest = self.check_retest_ema(df)
            
            if not (ema_breakout and ema_retest):
                return None
                
            # Определяем параметры сделки
            current_price = df['close'].iloc[-1]
            
            # Находим максимум после пробоя EMA33
            breakout_index = None
            for i in range(len(df)-10, len(df)):
                if (df.iloc[i-1]['close'] <= df.iloc[i-1]['ema33'] and 
                    df.iloc[i]['close'] > df.iloc[i]['ema33']):
                    breakout_index = i
                    break
                    
            if breakout_index is None:
                return None
                
            # Максимум после пробоя
            max_after_breakout = df['high'].iloc[breakout_index:].max()
            entry_price = max_after_breakout + (current_price * 0.001)  # +0.1%
            
            # Stop Loss (второй вариант - минимум после пробоя EMA33)
            min_after_breakout = df['low'].iloc[breakout_index:].min()
            stop_loss = min_after_breakout - (current_price * 0.001)  # -0.1%
            
            # Расчет расстояния риска
            risk_distance = entry_price - stop_loss
            
            # Расчет Take Profit уровней
            tp1 = entry_price + max(risk_distance, entry_price * 0.01)  # 1% или R/R 1:1
            tp2 = entry_price + (risk_distance * 2)  # R/R 1:2
            tp3 = entry_price + (risk_distance * 3)  # R/R 1:3
            
            # Проверяем валидность сигнала
            if (entry_price > current_price and 
                risk_distance > 0 and 
                risk_distance / entry_price < 0.03):  # Риск не более 3%
                
                return Signal(
                    symbol=ticker,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit_1=tp1,
                    take_profit_2=tp2,
                    take_profit_3=tp3,
                    signal_time=datetime.now(),
                    setup_description="EMA33 breakout with retest",
                    risk_reward_1=round((tp1 - entry_price) / risk_distance, 2),
                    risk_reward_2=round((tp2 - entry_price) / risk_distance, 2),
                    risk_reward_3=round((tp3 - entry_price) / risk_distance, 2)
                )
                
        except Exception as e:
            logger.error(f"Ошибка анализа {ticker}: {e}")
            return None

    async def send_telegram_message(self, message: str):
        """Отправка сообщения в Telegram"""
        try:
            await self.bot.send_message(
                chat_id=self.telegram_chat_id,
                text=message,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")

    def format_signal_message(self, signal: Signal) -> str:
        """Форматирование сообщения с сигналом"""
        risk_amount = signal.entry_price - signal.stop_loss
        
        message = f"""
🚀 <b>НОВЫЙ СИГНАЛ</b>

📊 <b>Инструмент:</b> {signal.symbol}
⏰ <b>Время:</b> {signal.signal_time.strftime('%H:%M:%S %d.%m.%Y')}

💡 <b>Сетап:</b> {signal.setup_description}

📈 <b>Параметры сделки:</b>
🎯 <b>Вход:</b> {signal.entry_price:.2f} ₽
🛑 <b>Stop Loss:</b> {signal.stop_loss:.2f} ₽
💰 <b>Риск:</b> {risk_amount:.2f} ₽ ({(risk_amount/signal.entry_price*100):.1f}%)

🎯 <b>Take Profit:</b>
• <b>TP1 (1/3):</b> {signal.take_profit_1:.2f} ₽ | R/R: 1:{signal.risk_reward_1}
• <b>TP2 (1/3):</b> {signal.take_profit_2:.2f} ₽ | R/R: 1:{signal.risk_reward_2}
• <b>TP3 (1/3):</b> {signal.take_profit_3:.2f} ₽ | R/R: 1:{signal.risk_reward_3}

📋 <b>Управление позицией:</b>
1️⃣ При достижении TP1 → закрыть 1/3 + SL в безубыток
2️⃣ При достижении TP2 → закрыть 1/3 + SL на уровень TP1
3️⃣ При достижении TP3 → закрыть остаток

#TradingSignal #{signal.symbol}
        """
        return message.strip()

    async def scan_instruments(self):
        """Сканирование инструментов на сигналы"""
        signals_found = 0
        
        for ticker in TOP_MOEX_STOCKS:
            try:
                if ticker not in self.instruments_cache:
                    continue
                    
                figi = self.instruments_cache[ticker]
                
                # Получаем 1-часовые свечи
                df = await self.get_candles(figi, CandleInterval.CANDLE_INTERVAL_HOUR, days=5)
                
                if df.empty:
                    continue
                    
                # Анализируем сетап
                signal = self.analyze_setup(ticker, df)
                
                if signal and ticker not in self.active_signals:
                    # Новый сигнал найден
                    self.active_signals[ticker] = signal
                    message = self.format_signal_message(signal)
                    await self.send_telegram_message(message)
                    signals_found += 1
                    logger.info(f"Новый сигнал: {ticker} @ {signal.entry_price}")
                    
                await asyncio.sleep(0.5)  # Пауза между запросами
                
            except Exception as e:
                logger.error(f"Ошибка сканирования {ticker}: {e}")
                continue
                
        if signals_found == 0:
            logger.info("Новых сигналов не найдено")

    async def monitor_active_signals(self):
        """Мониторинг активных сигналов"""
        for ticker, signal in list(self.active_signals.items()):
            try:
                if ticker not in self.instruments_cache:
                    continue
                    
                figi = self.instruments_cache[ticker]
                df = await self.get_candles(figi, CandleInterval.CANDLE_INTERVAL_1_MIN, days=1)
                
                if df.empty:
                    continue
                    
                current_price = df['close'].iloc[-1]
                
                # Проверяем срабатывание сигнала
                if current_price >= signal.entry_price:
                    message = f"""
🔥 <b>СИГНАЛ СРАБОТАЛ!</b>

📊 <b>{signal.symbol}</b>
💰 <b>Цена входа:</b> {signal.entry_price:.2f} ₽
📈 <b>Текущая цена:</b> {current_price:.2f} ₽

Позиция открыта! Следите за уровнями TP.
                    """
                    await self.send_telegram_message(message.strip())
                    
                # Проверяем достижение TP уровней
                if current_price >= signal.take_profit_1:
                    message = f"""
🎯 <b>TP1 ДОСТИГНУТ!</b>

📊 <b>{signal.symbol}</b>
💰 <b>TP1:</b> {signal.take_profit_1:.2f} ₽
📈 <b>Текущая цена:</b> {current_price:.2f} ₽

Закрыть 1/3 позиции и переставить SL в безубыток!
                    """
                    await self.send_telegram_message(message.strip())
                    
            except Exception as e:
                logger.error(f"Ошибка мониторинга {ticker}: {e}")

    async def cleanup_old_signals(self):
        """Очистка старых сигналов (старше 24 часов)"""
        current_time = datetime.now()
        to_remove = []
        
        for ticker, signal in self.active_signals.items():
            if current_time - signal.signal_time > timedelta(hours=24):
                to_remove.append(ticker)
                
        for ticker in to_remove:
            del self.active_signals[ticker]
            logger.info(f"Удален старый сигнал: {ticker}")

    async def run_scanner(self):
        """Основной цикл сканирования"""
        logger.info("Запуск сканера...")
        
        while True:
            try:
                # Работаем только в торговое время (10:00 - 18:30 МСК)
                current_hour = datetime.now().hour
                if 7 <= current_hour <= 15:  # UTC время (МСК-3)
                    await self.scan_instruments()
                    await self.monitor_active_signals()
                    await self.cleanup_old_signals()
                else:
                    logger.info("Вне торгового времени, ожидание...")
                    
            except Exception as e:
                logger.error(f"Ошибка в основном цикле: {e}")
                await self.send_telegram_message(f"⚠️ Ошибка в боте: {str(e)}")
                
            # Пауза между циклами сканирования
            await asyncio.sleep(300)  # 5 минут

# Основной файл для запуска
async def main():
    """Основная функция"""
    # Проверяем переменные окружения
    required_vars = ['TINKOFF_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"Отсутствуют переменные окружения: {missing_vars}")
        return
        
    bot = TradingBot()
    await bot.initialize()
    await bot.run_scanner()

if __name__ == "__main__":
    asyncio.run(main())
