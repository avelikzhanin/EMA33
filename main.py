import asyncio
import os
import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Set
import aiohttp
import json
from dataclasses import dataclass
from enum import Enum

# Telegram Bot
import telegram
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

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
    local_high: float
    local_low: float

class SignalStatus(Enum):
    WAITING = "waiting"
    TRIGGERED = "triggered"
    CLOSED = "closed"

# Топ-10 акций Мосбиржи
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
        
        self.application = Application.builder().token(self.telegram_token).build()
        self.active_signals: Dict[str, Signal] = {}
        self.instruments_cache: Dict[str, str] = {}
        self.subscribers: Set[int] = set()
        self.start_time = datetime.now()
        self.ema_breakouts: Dict[str, dict] = {}
        
        # Добавляем обработчики команд
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("subscribe", self.subscribe_command))
        self.application.add_handler(CommandHandler("unsubscribe", self.unsubscribe_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("signals", self.signals_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        welcome_message = """
🤖 <b>Добро пожаловать в Trading Bot!</b>

Я анализирую топ-10 акций Мосбиржи и отправляю торговые сигналы по стратегии пробоя EMA33.

<b>Доступные команды:</b>
/start - Показать это сообщение
/subscribe - Подписаться на сигналы
/unsubscribe - Отписаться от сигналов
/status - Статус бота и количество подписчиков
/signals - Показать активные сигналы
/stats - Статистика отслеживания
/help - Подробная помощь

<b>Для начала используйте:</b> /subscribe
        """
        await update.message.reply_text(welcome_message, parse_mode='HTML')

    async def subscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Подписка на сигналы"""
        user_id = update.effective_user.id
        if user_id not in self.subscribers:
            self.subscribers.add(user_id)
            await update.message.reply_text(
                "✅ <b>Вы подписались на торговые сигналы!</b>\n\n"
                "Теперь вы будете получать уведомления о новых сигналах и их статусе.",
                parse_mode='HTML'
            )
            logger.info(f"Новый подписчик: {user_id}")
        else:
            await update.message.reply_text("ℹ️ Вы уже подписаны на сигналы.")

    async def unsubscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отписка от сигналов"""
        user_id = update.effective_user.id
        if user_id in self.subscribers:
            self.subscribers.remove(user_id)
            await update.message.reply_text("❌ <b>Вы отписались от торговых сигналов.</b>", parse_mode='HTML')
            logger.info(f"Отписался: {user_id}")
        else:
            await update.message.reply_text("ℹ️ Вы не были подписаны на сигналы.")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Статус бота"""
        active_signals_count = len(self.active_signals)
        subscribers_count = len(self.subscribers)
        uptime = datetime.now() - self.start_time
        tracking_count = len(self.ema_breakouts)
        
        status_message = f"""
📊 <b>Статус бота:</b>

👥 <b>Подписчиков:</b> {subscribers_count}
🚨 <b>Активных сигналов:</b> {active_signals_count}
📍 <b>Отслеживаемых пробоев:</b> {tracking_count}
⏰ <b>Время работы:</b> {str(uptime).split('.')[0]}
📈 <b>Отслеживаемые акции:</b> {len(TOP_MOEX_STOCKS)}

<b>Инструменты:</b> {', '.join(TOP_MOEX_STOCKS)}
        """
        await update.message.reply_text(status_message, parse_mode='HTML')

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Статистика отслеживания пробоев"""
        if not self.ema_breakouts:
            await update.message.reply_text("📊 <b>Нет активных отслеживаний пробоев EMA33.</b>", parse_mode='HTML')
            return
            
        message = "📊 <b>Отслеживаемые пробои EMA33:</b>\n\n"
        for ticker, breakout_info in self.ema_breakouts.items():
            time_passed = datetime.now() - breakout_info['time']
            hours = time_passed.seconds // 3600
            minutes = (time_passed.seconds % 3600) // 60
            
            message += f"• <b>{ticker}</b>\n"
            message += f"  Пробой: {breakout_info['price']:.2f} ₽\n"
            message += f"  Время: {hours}ч {minutes}м назад\n"
            if 'local_high' in breakout_info:
                message += f"  Макс: {breakout_info['local_high']:.2f} ₽\n"
            if 'local_low' in breakout_info:
                message += f"  Мин: {breakout_info['local_low']:.2f} ₽\n"
            message += "\n"
            
        await update.message.reply_text(message, parse_mode='HTML')

    async def signals_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать активные сигналы"""
        if not self.active_signals:
            await update.message.reply_text("📭 <b>Активных сигналов нет.</b>", parse_mode='HTML')
            return

        message = "🔔 <b>Активные сигналы:</b>\n\n"
        for ticker, signal in self.active_signals.items():
            age = datetime.now() - signal.signal_time
            message += f"📊 <b>{ticker}</b>\n"
            message += f"💰 Вход: {signal.entry_price:.2f} ₽\n"
            message += f"🛑 SL: {signal.stop_loss:.2f} ₽\n"
            message += f"⏰ {age.seconds//3600}ч {(age.seconds//60)%60}м назад\n\n"

        await update.message.reply_text(message, parse_mode='HTML')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Подробная помощь"""
        help_message = """
📚 <b>Подробная информация о боте</b>

<b>🎯 Улучшенная стратегия торговли:</b>
1. Отскок от уровня поддержки
2. Пробой EMA33 вверх
3. Ожидание формирования локального максимума
4. Формирование локального минимума (ретест)
5. Отложенный ордер на пробой локального максимума

<b>⚙️ Параметры стратегии:</b>
• Время отслеживания после пробоя: до 48 часов
• Расстояние ретеста от EMA33: до 2%
• Минимальное движение для локальных экстремумов: 0.3%

<b>📊 Управление позицией:</b>
• TP1 (1/3): При R/R 1:1 → SL в безубыток
• TP2 (1/3): При R/R 1:2 → SL на уровень TP1
• TP3 (1/3): При R/R 1:3 → полное закрытие

<b>⏰ Режим работы:</b>
• Сканирование: каждые 5 минут
• Торговое время: 10:00-18:30 МСК
• Таймфрейм: 1 час

<b>📈 Отслеживаемые акции:</b>
SBER, GAZP, LKOH, YNDX, GMKN, NVTK, ROSN, MTSS, MGNT, PLZL

<b>⚠️ Важно:</b>
Бот предоставляет только информационные сигналы для анализа. Все торговые решения вы принимаете самостоятельно!
        """
        await update.message.reply_text(help_message, parse_mode='HTML')

    async def initialize(self):
        """Инициализация бота и кэширование инструментов"""
        try:
            async with Client(self.tinkoff_token) as client:
                instruments = await client.instruments.shares()
                for instrument in instruments.instruments:
                    if instrument.ticker in TOP_MOEX_STOCKS:
                        self.instruments_cache[instrument.ticker] = instrument.figi
                        
            logger.info(f"Инициализация завершена. Найдено инструментов: {len(self.instruments_cache)}")
            
        except Exception as e:
            logger.error(f"Ошибка инициализации: {e}")

    async def broadcast_message(self, message: str):
        """Отправка сообщения всем подписчикам"""
        if not self.subscribers:
            return
            
        failed_sends = []
        for chat_id in self.subscribers.copy():
            try:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить сообщение {chat_id}: {e}")
                failed_sends.append(chat_id)
                
        for chat_id in failed_sends:
            self.subscribers.discard(chat_id)

    async def get_candles(self, figi: str, interval: CandleInterval, days: int = 2) -> pd.DataFrame:
        """Получение свечных данных"""
        try:
            with Client(self.tinkoff_token) as client:
                from_time = now() - timedelta(days=days)
                to_time = now()
                
                candles = client.market_data.get_candles(
                    figi=figi,
                    from_=from_time,
                    to=to_time,
                    interval=interval
                )
                
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

    def find_local_extremes(self, df: pd.DataFrame, window: int = 3) -> Tuple[List[float], List[float]]:
        """Поиск локальных максимумов и минимумов"""
        highs = []
        lows = []
        
        for i in range(window, len(df) - window):
            if df['high'].iloc[i] == df['high'].iloc[i-window:i+window+1].max():
                highs.append((i, df['high'].iloc[i]))
            
            if df['low'].iloc[i] == df['low'].iloc[i-window:i+window+1].min():
                lows.append((i, df['low'].iloc[i]))
                
        return highs, lows

    def detect_ema_breakout(self, df: pd.DataFrame, ema_period: int = 33) -> Optional[dict]:
        """Обнаружение пробоя EMA33 вверх"""
        if len(df) < ema_period + 5:
            return None
            
        df['ema33'] = self.calculate_ema(df['close'], ema_period)
        
        for i in range(-10, 0):
            try:
                if (df.iloc[i-1]['close'] <= df.iloc[i-1]['ema33'] and 
                    df.iloc[i]['close'] > df.iloc[i]['ema33'] and
                    df.iloc[i]['volume'] > df['volume'].iloc[i-10:i].mean() * 1.2):
                    
                    return {
                        'index': len(df) + i,
                        'time': df.index[i],
                        'price': df.iloc[i]['close'],
                        'ema_value': df.iloc[i]['ema33']
                    }
            except:
                continue
                
        return None

    def check_setup_formation(self, df: pd.DataFrame, breakout_info: dict) -> Optional[dict]:
        """Проверка формирования сетапа после пробоя EMA33"""
        try:
            if not breakout_info:
                return None
                
            breakout_index = breakout_info['index']
            post_breakout_df = df.iloc[breakout_index:]
            
            if len(post_breakout_df) < 5:
                return None
                
            post_breakout_df['ema33'] = self.calculate_ema(df['close'], 33).iloc[breakout_index:]
            highs, lows = self.find_local_extremes(post_breakout_df, window=2)
            
            if not highs or not lows:
                return None
                
            local_high = None
            local_high_idx = None
            for idx, high_price in highs:
                if high_price > breakout_info['price'] * 1.003:
                    local_high = high_price
                    local_high_idx = idx
                    break
                    
            if not local_high:
                return None
                
            local_low = None
            local_low_idx = None
            for idx, low_price in lows:
                if idx > local_high_idx:
                    ema_at_low = post_breakout_df['ema33'].iloc[idx]
                    distance_to_ema = abs(low_price - ema_at_low) / ema_at_low
                    
                    if distance_to_ema <= 0.02:
                        local_low = low_price
                        local_low_idx = idx
                        break
                        
            if not local_low:
                return None
                
            if local_low < post_breakout_df['ema33'].iloc[local_low_idx] * 0.995:
                return None
                
            return {
                'local_high': local_high,
                'local_low': local_low,
                'ema_at_low': post_breakout_df['ema33'].iloc[local_low_idx],
                'current_price': df['close'].iloc[-1]
            }
            
        except Exception as e:
            logger.error(f"Ошибка проверки формирования сетапа: {e}")
            return None

    def generate_signal(self, ticker: str, setup_info: dict) -> Signal:
        """Генерация торгового сигнала"""
        entry_price = setup_info['local_high'] + (setup_info['local_high'] * 0.001)
        stop_loss = setup_info['local_low'] - (setup_info['local_low'] * 0.002)
        alt_stop_loss = setup_info['ema_at_low'] - (setup_info['ema_at_low'] * 0.005)
        stop_loss = max(stop_loss, alt_stop_loss)
        
        risk_distance = entry_price - stop_loss
        
        tp1 = entry_price + risk_distance * 1.0
        tp2 = entry_price + risk_distance * 2.0
        tp3 = entry_price + risk_distance * 3.0
        
        return Signal(
            symbol=ticker,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            signal_time=datetime.now(),
            setup_description="EMA33 breakout + Local High/Low formation",
            risk_reward_1=1.0,
            risk_reward_2=2.0,
            risk_reward_3=3.0,
            local_high=setup_info['local_high'],
            local_low=setup_info['local_low']
        )

    def format_signal_message(self, signal: Signal) -> str:
        """Форматирование сообщения с сигналом"""
        risk_amount = signal.entry_price - signal.stop_loss
        
        message = f"""
🚀 <b>НОВЫЙ СИГНАЛ</b>

📊 <b>Инструмент:</b> {signal.symbol}
⏰ <b>Время:</b> {signal.signal_time.strftime('%H:%M:%S %d.%m.%Y')}

💡 <b>Сетап:</b> {signal.setup_description}
📈 <b>Локальный максимум:</b> {signal.local_high:.2f} ₽
📉 <b>Локальный минимум:</b> {signal.local_low:.2f} ₽

📈 <b>Параметры сделки:</b>
🎯 <b>Вход (лимитный ордер):</b> {signal.entry_price:.2f} ₽
🛑 <b>Stop Loss:</b> {signal.stop_loss:.2f} ₽
💰 <b>Риск:</b> {risk_amount:.2f} ₽ ({(risk_amount/signal.entry_price*100):.1f}%)

🎯 <b>Take Profit:</b>
• <b>TP1 (1/3):</b> {signal.take_profit_1:.2f} ₽ | R/R: 1:1
• <b>TP2 (1/3):</b> {signal.take_profit_2:.2f} ₽ | R/R: 1:2
• <b>TP3 (1/3):</b> {signal.take_profit_3:.2f} ₽ | R/R: 1:3

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
                df = await self.get_candles(figi, CandleInterval.CANDLE_INTERVAL_HOUR, days=7)
                
                if df.empty or len(df) < 50:
                    continue
                
                if ticker not in self.ema_breakouts:
                    breakout_info = self.detect_ema_breakout(df)
                    if breakout_info:
                        self.ema_breakouts[ticker] = {
                            'time': breakout_info['time'],
                            'price': breakout_info['price'],
                            'index': breakout_info['index']
                        }
                        logger.info(f"Обнаружен пробой EMA33 для {ticker} @ {breakout_info['price']:.2f}")
                        
                        message = f"""
📍 <b>Пробой EMA33!</b>

📊 <b>{ticker}</b>
💰 Цена пробоя: {breakout_info['price']:.2f} ₽
⏰ Время: {breakout_info['time'].strftime('%H:%M')}

Отслеживаю формирование сетапа...
                        """
                        await self.broadcast_message(message.strip())
                
                if ticker in self.ema_breakouts and ticker not in self.active_signals:
                    breakout_data = self.ema_breakouts[ticker]
                    
                    time_since_breakout = datetime.now() - breakout_data['time']
                    if time_since_breakout > timedelta(hours=48):
                        del self.ema_breakouts[ticker]
                        logger.info(f"Удален устаревший пробой для {ticker}")
                        continue
                    
                    setup_info = self.check_setup_formation(df, breakout_data)
                    
                    if setup_info:
                        self.ema_breakouts[ticker]['local_high'] = setup_info['local_high']
                        self.ema_breakouts[ticker]['local_low'] = setup_info['local_low']
                        
                        signal = self.generate_signal(ticker, setup_info)
                        
                        if signal.entry_price > setup_info['current_price']:
                            self.active_signals[ticker] = signal
                            message = self.format_signal_message(signal)
                            await self.broadcast_message(message)
                            signals_found += 1
                            logger.info(f"Новый сигнал: {ticker} @ {signal.entry_price:.2f}")
                            
                            del self.ema_breakouts[ticker]
                    
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"Ошибка сканирования {ticker}: {e}")
                continue
                
        if signals_found > 0:
            logger.info(f"Найдено новых сигналов: {signals_found}")

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
                high_price = df['high'].iloc[-1]
                
                if high_price >= signal.entry_price and not hasattr(signal, 'triggered'):
                    signal.triggered = True
                    message = f"""
🔥 <b>СИГНАЛ СРАБОТАЛ!</b>

📊 <b>{signal.symbol}</b>
💰 <b>Цена входа:</b> {signal.entry_price:.2f} ₽
📈 <b>Текущая цена:</b> {current_price:.2f} ₽

Позиция открыта! Следите за уровнями TP и SL.
                    """
                    await self.broadcast_message(message.strip())
                    
                if hasattr(signal, 'triggered'):
                    if current_price >= signal.take_profit_1 and not hasattr(signal, 'tp1_reached'):
                        signal.tp1_reached = True
                        message = f"""
🎯 <b>TP1 ДОСТИГНУТ!</b>

📊 <b>{signal.symbol}</b>
💰 <b>TP1:</b> {signal.take_profit_1:.2f} ₽
📈 <b>Текущая цена:</b> {current_price:.2f} ₽
📊 <b>Прибыль:</b> {((signal.take_profit_1 - signal.entry_price) / signal.entry_price * 100):.1f}%

✅ Закрыть 1/3 позиции
✅ Переставить SL в безубыток ({signal.entry_price:.2f} ₽)
                        """
                        await self.broadcast_message(message.strip())
                    
                    if current_price >= signal.take_profit_2 and not hasattr(signal, 'tp2_reached'):
                        signal.tp2_reached = True
                        message = f"""
🎯 <b>TP2 ДОСТИГНУТ!</b>

📊 <b>{signal.symbol}</b>
💰 <b>TP2:</b> {signal.take_profit_2:.2f} ₽
📈 <b>Текущая цена:</b> {current_price:.2f} ₽
📊 <b>Прибыль:</b> {((signal.take_profit_2 - signal.entry_price) / signal.entry_price * 100):.1f}%

✅ Закрыть еще 1/3 позиции
✅ Переставить SL на уровень TP1 ({signal.take_profit_1:.2f} ₽)
                        """
                        await self.broadcast_message(message.strip())
                    
                    if current_price >= signal.take_profit_3 and not hasattr(signal, 'tp3_reached'):
                        signal.tp3_reached = True
                        message = f"""
🎯 <b>TP3 ДОСТИГНУТ! ПОЗИЦИЯ ЗАКРЫТА!</b>

📊 <b>{signal.symbol}</b>
💰 <b>TP3:</b> {signal.take_profit_3:.2f} ₽
📈 <b>Финальная цена:</b> {current_price:.2f} ₽
📊 <b>Общая прибыль:</b> {((signal.take_profit_3 - signal.entry_price) / signal.entry_price * 100):.1f}%

✅ Позиция полностью закрыта с прибылью!
                        """
                        await self.broadcast_message(message.strip())
                        del self.active_signals[ticker]
                        continue
                    
                    if current_price <= signal.stop_loss:
                        message = f"""
🛑 <b>STOP LOSS СРАБОТАЛ!</b>

📊 <b>{signal.symbol}</b>
💔 <b>Stop Loss:</b> {signal.stop_loss:.2f} ₽
📉 <b>Текущая цена:</b> {current_price:.2f} ₽
📊 <b>Убыток:</b> {((signal.stop_loss - signal.entry_price) / signal.entry_price * 100):.1f}%

❌ Позиция закрыта по стоп-лоссу.
                        """
                        await self.broadcast_message(message.strip())
                        del self.active_signals[ticker]
                        
            except Exception as e:
                logger.error(f"Ошибка мониторинга {ticker}: {e}")

    async def cleanup_old_signals(self):
        """Очистка старых сигналов (старше 48 часов)"""
        current_time = datetime.now()
        to_remove = []
        
        for ticker, signal in self.active_signals.items():
            if current_time - signal.signal_time > timedelta(hours=48):
                to_remove.append(ticker)
                
        for ticker in to_remove:
            del self.active_signals[ticker]
            logger.info(f"Удален старый сигнал: {ticker}")
            
            message = f"""
⏰ <b>Сигнал истек</b>

📊 <b>{ticker}</b>
Сигнал не сработал в течение 48 часов и был удален.
            """
            await self.broadcast_message(message.strip())

    async def cleanup_old_breakouts(self):
        """Очистка старых пробоев EMA33"""
        current_time = datetime.now()
        to_remove = []
        
        for ticker, breakout_info in self.ema_breakouts.items():
            if current_time - breakout_info['time'] > timedelta(hours=48):
                to_remove.append(ticker)
                
        for ticker in to_remove:
            del self.ema_breakouts[ticker]
            logger.info(f"Удален старый пробой EMA33: {ticker}")

    async def run_scanner(self):
        """Основной цикл сканирования"""
        logger.info("Запуск сканера...")
        
        while True:
            try:
                current_hour = datetime.now().hour
                current_minute = datetime.now().minute
                
                # UTC время (МСК-3)
                if 7 <= current_hour <= 15 or (current_hour == 15 and current_minute <= 30):
                    logger.info(f"Сканирование... Время: {datetime.now().strftime('%H:%M:%S')}")
                    logger.info(f"Активных сигналов: {len(self.active_signals)}, Отслеживаемых пробоев: {len(self.ema_breakouts)}")
                    
                    await self.scan_instruments()
                    await self.monitor_active_signals()
                    await self.cleanup_old_signals()
                    await self.cleanup_old_breakouts()
                else:
                    logger.info("Вне торгового времени, ожидание...")
                    
            except Exception as e:
                logger.error(f"Ошибка в основном цикле: {e}")
                
            await asyncio.sleep(300)  # 5 минут

    async def start_bot(self):
        """Запуск Telegram бота"""
        await self.application.initialize()
        await self.application.start()
        
        startup_message = """
🟢 <b>Бот запущен!</b>

Сканирование рынка активно.
Используйте /help для получения информации о стратегии.
        """
        await self.broadcast_message(startup_message.strip())
        
        polling_task = asyncio.create_task(self.application.updater.start_polling())
        scanner_task = asyncio.create_task(self.run_scanner())
        
        try:
            await asyncio.gather(polling_task, scanner_task)
        except KeyboardInterrupt:
            logger.info("Получен сигнал остановки")
        finally:
            await self.application.stop()

async def main():
    """Основная функция"""
    # Небольшая задержка для предотвращения конфликтов при перезапуске
    await asyncio.sleep(2)
    
    required_vars = ['TINKOFF_TOKEN', 'TELEGRAM_BOT_TOKEN']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"Отсутствуют переменные окружения: {missing_vars}")
        logger.info("Установите переменные окружения:")
        logger.info("TINKOFF_TOKEN - токен для Tinkoff Invest API")
        logger.info("TELEGRAM_BOT_TOKEN - токен вашего Telegram бота")
        return
        
    bot = TradingBot()
    
    # Пробуем инициализировать, но продолжаем даже при ошибке
    try:
        await bot.initialize()
    except Exception as e:
        logger.warning(f"Ошибка при инициализации Tinkoff API: {e}")
        logger.info("Бот продолжит работу без данных о инструментах")
    
    logger.info("=" * 50)
    logger.info("Trading Bot v2.0 - Улучшенная стратегия")
    logger.info("=" * 50)
    logger.info(f"Найдено инструментов: {len(bot.instruments_cache)}")
    if bot.instruments_cache:
        logger.info(f"Отслеживаемые тикеры: {', '.join(bot.instruments_cache.keys())}")
    else:
        logger.warning("Инструменты не загружены. Проверьте TINKOFF_TOKEN")
    logger.info("=" * 50)
    
    await bot.start_bot()

if __name__ == "__main__":
    asyncio.run(main())
