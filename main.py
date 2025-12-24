import argparse
import asyncio
import csv
import json
import os
import sqlite3
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import ChatForbidden, ChatWriteForbidden, FloodWait
from pyrogram.types import Message, Chat


class ChatAnalyzer:
    def __init__(self, session_name: str, api_id: int, api_hash: str):
        self.client = Client(session_name, api_id=api_id, api_hash=api_hash)
        self.db_conn = None
        self.chat_id = None
        self.start_date = None
        self.end_date = None

    def _get_date_filter_query(self, base_query: str) -> Tuple[str, list]:
        """Добавляет условия фильтрации по датам к SQL запросу"""
        params = []
        query = base_query

        if self.start_date:
            query += " AND date >= ?"
            params.append(self.start_date.isoformat())

        if self.end_date:
            # Добавляем 1 день, чтобы включить все сообщения последнего дня
            end_date_inclusive = self.end_date + timedelta(days=1)
            query += " AND date < ?"
            params.append(end_date_inclusive.isoformat())

        return query, params

    def init_db(self, db_path: str):
        """Инициализирует SQLite базу данных"""
        self.db_conn = sqlite3.connect(db_path)
        cursor = self.db_conn.cursor()

        # Создаем таблицу с индексами для улучшения производительности
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                date TEXT,
                sender_id INTEGER,
                sender_username TEXT,
                sender_first_name TEXT,
                sender_last_name TEXT,
                text TEXT,
                media_type TEXT,
                sticker_emoji TEXT,
                sticker_file_id TEXT,
                sticker_set_name TEXT
            )
        ''')

        # Создаем индексы
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sender_id ON messages(sender_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_date ON messages(date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_media_type ON messages(media_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sticker_file_id ON messages(sticker_file_id)')

        self.db_conn.commit()

    def parse_date(self, date_str: str) -> datetime:
        """Парсит дату в формате день.месяц.год и возвращает datetime с UTC временной зоной"""
        try:
            day, month, year = map(int, date_str.split('.'))
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"Некорректный формат даты: {date_str}. Используйте формат: день.месяц.год")

    async def fetch_messages(self):
        """Собирает все сообщения из выбранного чата"""
        print("Начинаю загрузку сообщений...")

        # Проверяем доступ к чату
        try:
            chat = await self.client.get_chat(self.chat_id)
        except Exception as e:
            print(f"Ошибка при получении информации о чате: {e}")
            return

        count = 0
        batch_count = 0
        batch_size = 100  # Размер батча для коммита в БД

        try:
            async for message in self.client.get_chat_history(self.chat_id):
                # В Pyrogram 2.x+ message.date уже является объектом datetime с часовым поясом
                message_date = message.date

                # Приводим к UTC если необходимо
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                else:
                    message_date = message_date.astimezone(timezone.utc)

                # Если указан период, проверяем вхождение в диапазон
                if self.start_date and message_date < self.start_date:
                    continue  # Пропускаем сообщения до начала периода
                if self.end_date and message_date > self.end_date:
                    continue  # Пропускаем сообщения после окончания периода

                self._save_message(message)
                count += 1
                batch_count += 1

                if batch_count >= batch_size:
                    print(f"Загружено {count} сообщений...")
                    self.db_conn.commit()  # Регулярно сохраняем данные
                    batch_count = 0

                    # Делаем небольшую паузу для избежания флуд-ограничений
                    await asyncio.sleep(0.1)

            # Финальный коммит
            self.db_conn.commit()
            print(f"Всего загружено {count} сообщений")

        except FloodWait as e:
            print(f"Получен FloodWait: ждем {e.x} секунд")
            await asyncio.sleep(e.x)
        except Exception as e:
            print(f"Ошибка при загрузке сообщений: {e}")
            import traceback
            traceback.print_exc()


    def _save_message(self, message: Message):
        """Сохраняет сообщение в базу данных"""
        cursor = self.db_conn.cursor()
        text = message.text or message.caption

        # Определение типа медиа
        media_type = None
        emoji = None
        file_id = None
        set_name = None

        if message.sticker:
            media_type = "sticker"
            emoji = message.sticker.emoji
            file_id = message.sticker.file_id
            set_name = message.sticker.set_name
        elif message.photo:
            media_type = "photo"
            file_id = message.photo.file_id
        elif message.video:
            media_type = "video"
            file_id = message.video.file_id
        elif message.voice:
            media_type = "voice"
            file_id = message.voice.file_id
        elif message.video_note:
            media_type = "video_note"
            file_id = message.video_note.file_id

        # В Pyrogram 2.x+ message.date уже является объектом datetime
        # Приводим к UTC и преобразуем в ISO формат
        message_date = message.date.astimezone(timezone.utc).isoformat()

        cursor.execute(
            "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message_date,  # Используем преобразованную дату
                message.from_user.id if message.from_user else None,
                message.from_user.username if message.from_user else None,
                message.from_user.first_name if message.from_user else None,
                message.from_user.last_name if message.from_user else None,
                text,
                media_type,
                emoji,
                file_id,
                set_name
            )
        )

    def analyze_global_stats(self) -> Dict:
        """Анализирует глобальную статистику чата"""
        cursor = self.db_conn.cursor()

        # 1. Всего сообщений (с фильтрацией по периоду)
        query = "SELECT COUNT(*) FROM messages WHERE 1=1"
        query, params = self._get_date_filter_query(query)
        cursor.execute(query, params)
        total_messages = cursor.fetchone()[0]

        # 2. Топ-10 самых популярных слов (с фильтрацией по периоду)
        query = "SELECT text FROM messages WHERE text IS NOT NULL"
        query, params = self._get_date_filter_query(query)
        cursor.execute(query, params)
        all_texts = [row[0] for row in cursor.fetchall()]
        words = []
        stop_words = {
            'и', 'в', 'на', 'с', 'по', 'за', 'до', 'о', 'у', 'а', 'но', 'или', 'же',
            'то', 'не', 'что', 'как', 'это', 'бы', 'был', 'была', 'было', 'так', 'вот',
            'же', 'ли', 'же', 'же', 'же', 'же', 'же', 'же', 'же', 'же', 'же', 'же',
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of',
            'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'have', 'has', 'had',
            'will', 'would', 'could', 'should', 'may', 'might', 'can', 'this', 'that', 'these', 'those'
        }

        for text in all_texts:
            if text:
                # Извлекаем слова длиной >= 2 и не входящие в стоп-слова
                text_words = re.findall(r'\b\w{2,}\b', text.lower())
                words.extend([word for word in text_words if word not in stop_words])

        top_words = Counter(words).most_common(10)

        # 3. Топ-5 самых популярных стикеров (по эмодзи, а не file_id) с фильтрацией по периоду
        query = "SELECT sticker_emoji, sticker_set_name FROM messages WHERE sticker_emoji IS NOT NULL"
        query, params = self._get_date_filter_query(query)
        cursor.execute(query, params)
        sticker_data = [(row[0], row[1]) for row in cursor.fetchall() if row[0]]
        sticker_emojis = [row[0] for row in sticker_data]
        top_stickers = Counter(sticker_emojis).most_common(5)

        # Получаем дополнительную информацию о топ-стикерах
        detailed_top_stickers = []
        for emoji, count in top_stickers:
            # Получаем имя стикерпака для этого эмодзи (с фильтрацией по периоду)
            query = "SELECT sticker_set_name FROM messages WHERE sticker_emoji = ?"
            query, params = self._get_date_filter_query(query)
            query += "LIMIT 1"
            params = [emoji] + params
            cursor.execute(query, params)
            result = cursor.fetchone()
            if result and result[0]:
                set_name = result[0]
                sticker_pack_link = f"tg://addstickers?set={set_name}"
            else:
                set_name = None
                sticker_pack_link = None
            detailed_top_stickers.append((emoji, count, set_name, sticker_pack_link))

        # 4. Самый активный день (учитываем только текстовые сообщения, без фото и видео) с фильтрацией
        query = "SELECT date FROM messages WHERE date IS NOT NULL AND media_type NOT IN ('photo', 'video')"
        query, params = self._get_date_filter_query(query)
        cursor.execute(query, params)
        # ИСПРАВЛЕНО: теперь правильно парсим дату из ISO строки
        date_strings = [row[0] for row in cursor.fetchall()]
        dates = []
        for dt_str in date_strings:
            try:
                dt = datetime.fromisoformat(dt_str).astimezone(timezone.utc)
                dates.append(dt.date())
            except ValueError:
                # Пропускаем некорректные строки дат
                continue
        active_day = Counter(dates).most_common(1)[0] if dates else (None, 0)

        # 5. Дней без активности (учитываем весь период от первого до последнего сообщения)
        if dates:
            start_date = min(dates)
            end_date = max(dates)
            total_days = (end_date - start_date).days + 1
            active_days = len(set(dates))
            inactive_days = total_days - active_days
        else:
            inactive_days = 0

        # 6. Общее количество голосовых/кружочков с фильтрацией
        query = "SELECT COUNT(*) FROM messages WHERE media_type IN ('voice', 'video_note')"
        query, params = self._get_date_filter_query(query)
        cursor.execute(query, params)
        voice_count = cursor.fetchone()[0]

        # 7. Самый активный участник по сообщениям (по ID, а не по имени) с фильтрацией
        query = """
            SELECT sender_id, sender_first_name, COUNT(*) 
            FROM messages 
            WHERE sender_id IS NOT NULL
        """
        query, params = self._get_date_filter_query(query)
        query += " GROUP BY sender_id ORDER BY COUNT(*) DESC LIMIT 1"
        cursor.execute(query, params)
        top_sender = cursor.fetchone()

        # 8. Самый активный по голосовым (по ID) с фильтрацией
        query = """
            SELECT sender_id, sender_first_name, COUNT(*) 
            FROM messages 
            WHERE sender_id IS NOT NULL AND media_type IN ('voice', 'video_note')
        """
        query, params = self._get_date_filter_query(query)
        query += " GROUP BY sender_id ORDER BY COUNT(*) DESC LIMIT 1"
        cursor.execute(query, params)
        top_voice_sender = cursor.fetchone()

        # 9. Самый активный по фото/видео (по ID) с фильтрацией
        query = """
            SELECT sender_id, sender_first_name, COUNT(*) 
            FROM messages 
            WHERE sender_id IS NOT NULL AND media_type IN ('photo', 'video')
        """
        query, params = self._get_date_filter_query(query)
        query += " GROUP BY sender_id ORDER BY COUNT(*) DESC LIMIT 1"
        cursor.execute(query, params)
        top_media_sender = cursor.fetchone()

        # 10. Самый активный по словам (по ID) с фильтрацией
        word_counts = defaultdict(int)
        query = "SELECT text, sender_id FROM messages WHERE text IS NOT NULL AND sender_id IS NOT NULL"
        query, params = self._get_date_filter_query(query)
        cursor.execute(query, params)

        for text, user_id in cursor.fetchall():
            if user_id and text:
                # Учитываем только слова длиной >= 2 и не входящие в стоп-слова
                text_words = re.findall(r'\b\w{2,}\b', text.lower())
                filtered_words = [word for word in text_words if word not in stop_words]
                word_counts[user_id] += len(filtered_words)
        top_wordy_sender = max(word_counts.items(), key=lambda x: x[1]) if word_counts else (None, 0)

        # 11. Самый активный по стикерам (по ID) с фильтрацией
        query = """
            SELECT sender_id, sender_first_name, COUNT(*) 
            FROM messages 
            WHERE sender_id IS NOT NULL AND sticker_emoji IS NOT NULL
        """
        query, params = self._get_date_filter_query(query)
        query += " GROUP BY sender_id ORDER BY COUNT(*) DESC LIMIT 1"
        cursor.execute(query, params)
        top_sticker_sender = cursor.fetchone()

        # 12. Месяцы по активности (с фильтрацией по периоду)
        month_activity = defaultdict(int)
        query = "SELECT date FROM messages WHERE date IS NOT NULL"
        query, params = self._get_date_filter_query(query)
        cursor.execute(query, params)

        # ИСПРАВЛЕНО: теперь правильно парсим дату из ISO строки
        date_strings = [row[0] for row in cursor.fetchall()]
        for dt_str in date_strings:
            try:
                dt = datetime.fromisoformat(dt_str).astimezone(timezone.utc)
                month = dt.strftime('%Y-%m')
                month_activity[month] += 1
            except ValueError:
                # Пропускаем некорректные строки дат
                continue
        monthly_ranking = sorted(month_activity.items(), key=lambda x: x[1], reverse=True)

        # 13. Популярные слова по месяцам (только для месяцев в выбранном периоде)
        monthly_top_words = {}
        for month in month_activity.keys():
            # Вычисляем диапазон дат для месяца
            year, mon = map(int, month.split('-'))
            month_start = datetime(year, mon, 1, tzinfo=timezone.utc).isoformat()

            # Следующий месяц
            if mon == 12:
                next_month_start = datetime(year + 1, 1, 1, tzinfo=timezone.utc).isoformat()
            else:
                next_month_start = datetime(year, mon + 1, 1, tzinfo=timezone.utc).isoformat()

            # Запрос с фильтрацией по периоду И по месяцу
            base_query = """
                SELECT text FROM messages 
                WHERE date >= ? AND date < ?
            """
            query, params = self._get_date_filter_query(base_query)
            params = [month_start, next_month_start] + params

            cursor.execute(query, params)
            texts = [row[0] for row in cursor.fetchall() if row[0]]
            all_month_words = []
            for text in texts:
                if text:
                    # Учитываем только слова длиной >= 2 и не входящие в стоп-слова
                    text_words = re.findall(r'\b\w{2,}\b', text.lower())
                    filtered_words = [word for word in text_words if word not in stop_words]
                    all_month_words.extend(filtered_words)
            if all_month_words:
                monthly_top_words[month] = Counter(all_month_words).most_common(1)[0][0]

        # 14. Самые активные участники по месяцам (с фильтрацией)
        monthly_top_senders = {}
        for month in month_activity.keys():
            year, mon = map(int, month.split('-'))
            month_start = datetime(year, mon, 1, tzinfo=timezone.utc).isoformat()

            if mon == 12:
                next_month_start = datetime(year + 1, 1, 1, tzinfo=timezone.utc).isoformat()
            else:
                next_month_start = datetime(year, mon + 1, 1, tzinfo=timezone.utc).isoformat()

            base_query = """
                SELECT sender_id, sender_first_name, COUNT(*) FROM messages 
                WHERE date >= ? AND date < ? AND sender_id IS NOT NULL
            """
            query, params = self._get_date_filter_query(base_query)
            query+="GROUP BY sender_id ORDER BY COUNT(*) DESC LIMIT 1"
            params = [month_start, next_month_start] + params
            cursor.execute(query, params)
            result = cursor.fetchone()
            monthly_top_senders[month] = (result[1] if result else None, result[0] if result else None)  # (имя, id)

        return {
            "total_messages": total_messages,
            "top_words": top_words,
            "top_stickers": detailed_top_stickers,
            "active_day": active_day,
            "inactive_days": inactive_days,
            "voice_count": voice_count,
            "top_sender": top_sender,
            "top_voice_sender": top_voice_sender,
            "top_media_sender": top_media_sender,
            "top_wordy_sender": top_wordy_sender,
            "top_sticker_sender": top_sticker_sender,
            "monthly_ranking": monthly_ranking,
            "monthly_top_words": monthly_top_words,
            "monthly_top_senders": monthly_top_senders
        }

    def analyze_user_stats(self, user_id: int) -> Dict:
        """Анализирует статистику конкретного пользователя"""
        cursor = self.db_conn.cursor()

        # 1. Всего сообщений (с фильтрацией по периоду)
        query = "SELECT COUNT(*) FROM messages WHERE sender_id = ?"
        params = [user_id]
        query, date_params = self._get_date_filter_query(query)
        params = params + date_params
        cursor.execute(query, params)
        total_messages = cursor.fetchone()[0]

        # 2. Топ-10 слов (с фильтрацией по периоду)
        query = "SELECT text FROM messages WHERE sender_id = ? AND text IS NOT NULL"
        params = [user_id]
        query, date_params = self._get_date_filter_query(query)
        params = params + date_params
        cursor.execute(query, params)
        user_texts = [row[0] for row in cursor.fetchall() if row[0]]
        all_user_words = []
        stop_words = {
            'и', 'в', 'на', 'с', 'по', 'за', 'до', 'о', 'у', 'а', 'но', 'или', 'же',
            'то', 'не', 'что', 'как', 'это', 'бы', 'был', 'была', 'было', 'так', 'вот',
            'же', 'ли', 'же', 'же', 'же', 'же', 'же', 'же', 'же', 'же', 'же', 'же',
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of',
            'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'have', 'has', 'had',
            'will', 'would', 'could', 'should', 'may', 'might', 'can', 'this', 'that', 'these', 'those'
        }

        for text in user_texts:
            if text:
                # Учитываем только слова длиной >= 2 и не входящие в стоп-слова
                text_words = re.findall(r'\b\w{2,}\b', text.lower())
                filtered_words = [word for word in text_words if word not in stop_words]
                all_user_words.extend(filtered_words)
        top_user_words = Counter(all_user_words).most_common(10)

        # 3. Самый популярный стикер (по эмодзи, а не file_id) с фильтрацией
        query = "SELECT sticker_emoji, sticker_set_name FROM messages WHERE sender_id = ? AND sticker_emoji IS NOT NULL"
        params = [user_id]
        query, date_params = self._get_date_filter_query(query)
        params = params + date_params
        cursor.execute(query, params)
        user_sticker_data = [(row[0], row[1]) for row in cursor.fetchall() if row[0]]
        user_stickers = [row[0] for row in user_sticker_data]
        if user_stickers:
            top_user_sticker_emoji = Counter(user_stickers).most_common(1)[0]
            # Получаем имя стикерпака для этого эмодзи (с фильтрацией)
            query = "SELECT sticker_set_name FROM messages WHERE sender_id = ? AND sticker_emoji = ?"
            params = [user_id, top_user_sticker_emoji[0]]
            query, date_params = self._get_date_filter_query(query)
            params = params + date_params
            cursor.execute(query, params)
            result = cursor.fetchone()
            if result and result[0]:
                set_name = result[0]
                sticker_pack_link = f"tg://addstickers?set={set_name}"
            else:
                set_name = None
                sticker_pack_link = None
            top_user_sticker = (top_user_sticker_emoji[0], top_user_sticker_emoji[1], set_name, sticker_pack_link)
        else:
            top_user_sticker = (None, 0, None, None)

        # 4. Самый активный день (учитываем только текстовые сообщения, без фото и видео) с фильтрацией
        query = "SELECT date FROM messages WHERE sender_id = ? AND date IS NOT NULL AND media_type NOT IN ('photo', 'video')"
        params = [user_id]
        query, date_params = self._get_date_filter_query(query)
        params = params + date_params
        cursor.execute(query, params)
        # ИСПРАВЛЕНО: теперь правильно парсим дату из ISO строки
        date_strings = [row[0] for row in cursor.fetchall()]
        user_dates = []
        for dt_str in date_strings:
            try:
                dt = datetime.fromisoformat(dt_str).astimezone(timezone.utc)
                user_dates.append(dt.date())
            except ValueError:
                # Пропускаем некорректные строки дат
                continue
        active_user_day = Counter(user_dates).most_common(1)[0] if user_dates else (None, 0)

        # 5. Дней без активности (в рамках общего периода)
        if user_dates:
            start_date = min(user_dates)
            end_date = max(user_dates)
            total_days = (end_date - start_date).days + 1
            active_days = len(set(user_dates))
            inactive_days = total_days - active_days
        else:
            inactive_days = 0

        # 6. Количество голосовых с фильтрацией
        query = "SELECT COUNT(*) FROM messages WHERE sender_id = ? AND media_type IN ('voice', 'video_note')"
        params = [user_id]
        query, date_params = self._get_date_filter_query(query)
        params = params + date_params
        cursor.execute(query, params)
        user_voice_count = cursor.fetchone()[0]

        # 7. Месяцы по активности (с фильтрацией)
        user_month_activity = defaultdict(int)
        query = "SELECT date FROM messages WHERE sender_id = ? AND date IS NOT NULL"
        params = [user_id]
        query, date_params = self._get_date_filter_query(query)
        params = params + date_params
        cursor.execute(query, params)

        # ИСПРАВЛЕНО: теперь правильно парсим дату из ISO строки
        date_strings = [row[0] for row in cursor.fetchall()]
        for dt_str in date_strings:
            try:
                dt = datetime.fromisoformat(dt_str).astimezone(timezone.utc)
                month = dt.strftime('%Y-%m')
                user_month_activity[month] += 1
            except ValueError:
                # Пропускаем некорректные строки дат
                continue
        user_monthly_ranking = sorted(user_month_activity.items(), key=lambda x: x[1], reverse=True)

        return {
            "total_messages": total_messages,
            "top_words": top_user_words,
            "top_sticker": top_user_sticker,
            "active_day": active_user_day,
            "inactive_days": inactive_days,
            "voice_count": user_voice_count,
            "monthly_ranking": user_monthly_ranking
        }
    def export_results(self, global_stats: Dict, user_stats: Dict[str, Dict], output_file: str):
        """Экспортирует результаты в текстовый файл"""
        with open(output_file, 'w', encoding='utf-8') as f:
            # Добавляем информацию о периоде анализа
            if self.start_date and self.end_date:
                start_str = self.start_date.strftime("%d.%m.%Y")
                end_str = self.end_date.strftime("%d.%m.%Y")
                f.write(f"=== АНАЛИЗ ЗА ПЕРИОД: {start_str} - {end_str} ===\n\n")
            elif self.start_date:
                start_str = self.start_date.strftime("%d.%m.%Y")
                f.write(f"=== АНАЛИЗ С {start_str} ===\n\n")
            elif self.end_date:
                end_str = self.end_date.strftime("%d.%m.%Y")
                f.write(f"=== АНАЛИЗ ДО {end_str} ===\n\n")
            else:
                f.write("=== АНАЛИЗ ЗА ВСЕ ВРЕМЯ ===\n\n")

            f.write("=== ГЛОБАЛЬНАЯ СТАТИСТИКА ===\n\n")

            f.write(f"1. Всего сообщений: {global_stats['total_messages']}\n\n")

            f.write("2. Топ-10 самых популярных слов:\n")
            for i, (word, count) in enumerate(global_stats['top_words'], 1):
                f.write(f"   {i}. {word}: {count}\n")
            f.write("\n")

            f.write("3. Топ-5 самых популярных стикеров:\n")
            for i, (emoji, count, set_name, link) in enumerate(global_stats['top_stickers'], 1):
                if emoji:
                    f.write(f"   {i}. {emoji} (Сет: {set_name})\n")
                    if link:
                        f.write(f"      Ссылка для добавления: {link}\n")
                    f.write(f"      Использований: {count}\n")
                else:
                    f.write(f"   {i}. Стикер не найден\n")
            f.write("\n")

            f.write(
                f"4. Самый активный день (без фото и видео): {global_stats['active_day'][0]} ({global_stats['active_day'][1]} сообщений)\n\n")

            f.write(f"5. Дней без активности: {global_stats['inactive_days']}\n\n")

            f.write(f"6. Общее количество голосовых/кружочков: {global_stats['voice_count']}\n\n")

            top_sender_name = global_stats['top_sender'][1] if global_stats['top_sender'] else "Нет данных"
            f.write(
                f"7. Самый активный участник (по сообщениям): {top_sender_name} (ID: {global_stats['top_sender'][0] if global_stats['top_sender'] else 'N/A'})\n\n")

            top_voice_name = global_stats['top_voice_sender'][1] if global_stats['top_voice_sender'] else "Нет данных"
            f.write(
                f"8. Самый активный по голосовым: {top_voice_name} (ID: {global_stats['top_voice_sender'][0] if global_stats['top_voice_sender'] else 'N/A'})\n\n")

            top_media_name = global_stats['top_media_sender'][1] if global_stats['top_media_sender'] else "Нет данных"
            f.write(
                f"9. Самый активный по фото/видео: {top_media_name} (ID: {global_stats['top_media_sender'][0] if global_stats['top_media_sender'] else 'N/A'})\n\n")

            f.write(
                f"10. Самый активный по словам: ID {global_stats['top_wordy_sender'][0]} ({global_stats['top_wordy_sender'][1]} слов)\n\n")

            top_sticker_name = global_stats['top_sticker_sender'][1] if global_stats[
                'top_sticker_sender'] else "Нет данных"
            f.write(
                f"11. Самый активный по стикерам: {top_sticker_name} (ID: {global_stats['top_sticker_sender'][0] if global_stats['top_sticker_sender'] else 'N/A'})\n\n")

            f.write("12. Месяцы по активности:\n")
            for i, (month, count) in enumerate(global_stats['monthly_ranking'], 1):
                f.write(f"   {i}. {month}: {count} сообщений\n")
            f.write("\n")

            f.write("13. Популярные слова по месяцам:\n")
            for month, word in global_stats['monthly_top_words'].items():
                f.write(f"   {month}: {word}\n")
            f.write("\n")

            f.write("14. Самые активные участники по месяцам:\n")
            for month, (name, user_id) in global_stats['monthly_top_senders'].items():
                f.write(f"   {month}: {name} (ID: {user_id})\n")
            f.write("\n")

            f.write("=== СТАТИСТИКА ПОЛЬЗОВАТЕЛЕЙ ===\n\n")
            for user_id, stats in user_stats.items():
                f.write(f"--- ID: {user_id} ---\n")
                f.write(f"1. Всего сообщений: {stats['total_messages']}\n")

                f.write("2. Топ-10 слов:\n")
                for i, (word, count) in enumerate(stats['top_words'], 1):
                    f.write(f"   {i}. {word}: {count}\n")

                emoji, count, set_name, link = stats['top_sticker']
                if emoji:
                    f.write(f"3. Самый популярный стикер: {emoji} (Сет: {set_name})\n")
                    if link:
                        f.write(f"   Ссылка для добавления: {link}\n")
                    f.write(f"   Использований: {count}\n")
                else:
                    f.write("3. Самый популярный стикер: Не найдено\n")

                f.write(
                    f"4. Самый активный день (без фото и видео): {stats['active_day'][0]} ({stats['active_day'][1]} сообщений)\n")

                f.write(f"5. Дней без активности: {stats['inactive_days']}\n")

                f.write(f"6. Количество голосовых: {stats['voice_count']}\n")

                f.write("7. Месяцы по активности:\n")
                for i, (month, count) in enumerate(stats['monthly_ranking'], 1):
                    f.write(f"   {i}. {month}: {count} сообщений\n")

                f.write("\n")

    async def get_sorted_chats(self):
        """Получает список чатов с группировкой и сортировкой"""
        chat_groups = {
            "channels": [],
            "groups": [],
            "private": [],
            "unnamed": []
        }

        # Получаем все диалоги
        dialogs = []
        try:
            async for dialog in self.client.get_dialogs():
                dialogs.append(dialog)
        except Exception as e:
            print(f"Ошибка при получении списка чатов: {e}")
            return chat_groups

        # Для каждого типа чата получаем последнее сообщение
        for dialog in dialogs:
            chat = dialog.chat
            last_message_date = dialog.top_message.date if dialog.top_message else None

            # Определяем тип чата
            if chat.type == "channel":
                chat_groups["channels"].append((chat.id, chat.title or "Без названия", last_message_date))
            elif chat.type in ["group", "supergroup"]:
                chat_groups["groups"].append((chat.id, chat.title or "Без названия", last_message_date))
            elif chat.type == "private":
                chat_groups["private"].append((chat.id, chat.title or "Без названия", last_message_date))
            else:
                # Если название отсутствует или пустое
                if not chat.title or chat.title == "Без названия":
                    chat_groups["unnamed"].append((chat.id, "Без названия", last_message_date))
                else:
                    # Если тип не определен, но есть название - относим к группе "другое"
                    chat_groups["groups"].append((chat.id, chat.title, last_message_date))

        # Сортируем каждую группу по дате последнего сообщения (новые первыми)
        for group in chat_groups.values():
            group.sort(key=lambda x: x[2] or datetime.min, reverse=True)

        return chat_groups

    async def run_interactive(self):
        """Запуск в интерактивном режиме"""
        await self.client.start()

        print("Доступные чаты:")
        chat_groups = await self.get_sorted_chats()

        # Выводим отсортированные и сгруппированные чаты
        if chat_groups["channels"]:
            print("\n--- КАНАЛЫ ---")
            for chat_id, title, last_msg_date in chat_groups["channels"]:
                date_str = last_msg_date.strftime("%Y-%m-%d %H:%M:%S") if last_msg_date else "Нет данных"
                print(f"{chat_id} - {title} (Посл. сообщение: {date_str})")

        if chat_groups["groups"]:
            print("\n--- ГРУППЫ ---")
            for chat_id, title, last_msg_date in chat_groups["groups"]:
                date_str = last_msg_date.strftime("%Y-%m-%d %H:%M:%S") if last_msg_date else "Нет данных"
                print(f"{chat_id} - {title} (Посл. сообщение: {date_str})")

        if chat_groups["private"]:
            print("\n--- ЛИЧНЫЕ ЧАТЫ ---")
            for chat_id, title, last_msg_date in chat_groups["private"]:
                date_str = last_msg_date.strftime("%Y-%m-%d %H:%M:%S") if last_msg_date else "Нет данных"
                print(f"{chat_id} - {title} (Посл. сообщение: {date_str})")

        if chat_groups["unnamed"]:
            print("\n--- БЕЗ НАЗВАНИЯ ---")
            for chat_id, title, last_msg_date in chat_groups["unnamed"]:
                date_str = last_msg_date.strftime("%Y-%m-%d %H:%M:%S") if last_msg_date else "Нет данных"
                print(f"{chat_id} - {title} (Посл. сообщение: {date_str})")

        while True:
            try:
                chat_id_input = input("\nВведите ID чата для анализа: ")
                self.chat_id = int(chat_id_input)
                break
            except ValueError:
                print("Некорректный ID чата. Пожалуйста, введите целое число.")

        # Запрашиваем период анализа
        print("\nВведите период анализа (формат: день.месяц.год)")
        print("Например: 1.1.2023 (для 1 января 2023 года)")
        print("Оставьте пустым, чтобы проанализировать все сообщения")

        start_input = input("Начальная дата (день.месяц.год): ").strip()
        end_input = input("Конечная дата (день.месяц.год): ").strip()

        if start_input:
            try:
                self.start_date = self.parse_date(start_input)
            except ValueError as e:
                print(f"Ошибка: {e}")
                return
        else:
            self.start_date = None

        if end_input:
            try:
                self.end_date = self.parse_date(end_input)
                # Проверяем корректность диапазона
                if self.start_date and self.end_date < self.start_date:
                    print("Ошибка: конечная дата не может быть раньше начальной")
                    return
            except ValueError as e:
                print(f"Ошибка: {e}")
                return
        else:
            self.end_date = None

        # Проверяем доступ к чату
        try:
            await self.client.get_chat(self.chat_id)
        except ChatForbidden:
            print("Доступ к чату запрещен. Возможно, вы покинули чат или были заблокированы.")
            return
        except Exception as e:
            print(f"Ошибка при доступе к чату: {e}")
            return

        db_path = f"chat_{abs(self.chat_id)}.db"
        self.init_db(db_path)

        await self.fetch_messages()

        print("Начинаю анализ...")
        global_stats = self.analyze_global_stats()

        # Получаем всех пользователей (без ограничения)
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT DISTINCT sender_id FROM messages WHERE sender_id IS NOT NULL")
        user_ids = [row[0] for row in cursor.fetchall()]

        user_stats = {}
        for user_id in user_ids:  # Убрано ограничение на 10
            user_stats[user_id] = self.analyze_user_stats(user_id)

        output_file = f"analysis_{abs(self.chat_id)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        self.export_results(global_stats, user_stats, output_file)

        print(f"Анализ завершен. Результаты сохранены в {output_file}")

    async def run_with_args(self, chat_id: int, start_date: str, end_date: str, db_path: str, output_file: str):
        """Запуск с аргументами командной строки"""
        await self.client.start()

        # Парсим даты
        if start_date:
            try:
                self.start_date = self.parse_date(start_date)
            except ValueError as e:
                print(f"Ошибка: {e}")
                return
        else:
            self.start_date = None

        if end_date:
            try:
                self.end_date = self.parse_date(end_date)
                # Проверяем корректность диапазона
                if self.start_date and self.end_date < self.start_date:
                    print("Ошибка: конечная дата не может быть раньше начальной")
                    return
            except ValueError as e:
                print(f"Ошибка: {e}")
                return
        else:
            self.end_date = None

        # Проверяем доступ к чату
        try:
            await self.client.get_chat(chat_id)
        except ChatForbidden:
            print("Доступ к чату запрещен. Возможно, вы покинули чат или были заблокированы.")
            return
        except Exception as e:
            print(f"Ошибка при доступе к чату: {e}")
            return

        self.chat_id = chat_id
        self.init_db(db_path)

        await self.fetch_messages()

        print("Начинаю анализ...")
        global_stats = self.analyze_global_stats()

        # Получаем всех пользователей (без ограничения)
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT DISTINCT sender_id FROM messages WHERE sender_id IS NOT NULL")
        user_ids = [row[0] for row in cursor.fetchall()]

        user_stats = {}
        for user_id in user_ids:  # Убрано ограничение на 10
            user_stats[user_id] = self.analyze_user_stats(user_id)

        self.export_results(global_stats, user_stats, output_file)

        print(f"Анализ завершен. Результаты сохранены в {output_file}")


def main():
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description="Telegram Chat Analyzer Userbot")
    parser.add_argument("--chat_id", type=int, help="ID чата для анализа")
    parser.add_argument("--start_date", type=str, help="Начальная дата в формате день.месяц.год (например: 1.1.2023)")
    parser.add_argument("--end_date", type=str, help="Конечная дата в формате день.месяц.год (например: 31.12.2023)")
    parser.add_argument("--db_path", type=str, default="chat_data.db", help="Путь к базе данных")
    parser.add_argument("--output", type=str, default="results.txt", help="Файл для вывода результатов")

    # Парсим только известные аргументы, игнорируя лишние
    args, unknown = parser.parse_known_args()

    # Загрузка конфигурации API из env файла или переменных окружения
    api_id = int(os.getenv("API_ID", 0))
    api_hash = os.getenv("API_HASH", "")

    if not api_id or not api_hash:
        print("Пожалуйста, укажите API_ID и API_HASH в переменных окружения")
        return

    analyzer = ChatAnalyzer("analyzer_session", api_id, api_hash)

    # Если не переданы аргументы (или передан только --help), запускаем интерактивный режим
    if not args.chat_id:
        asyncio.run(analyzer.run_interactive())
    else:
        asyncio.run(analyzer.run_with_args(args.chat_id, args.start_date, args.end_date, args.db_path, args.output))


if __name__ == "__main__":
    main()