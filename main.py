import os
import asyncio
import datetime
import logging
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import asyncpg

# Загрузка конфига
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID")

# Логирование
logging.basicConfig(level=logging.INFO)

# Инициализация бота
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СОСТОЯНИЯ (FSM) ---
class PostState(StatesGroup):
    choosing_category = State()
    waiting_text = State()
    waiting_photo = State()

# --- МАТ-ФИЛЬТР (ВАРИАНТ А+) ---
BAD_WORDS = ["хуй", "пизд", "ебал", "сука", "бля"] # Дополни список сам

def is_clean(text):
    if not text: return True
    # Убираем всё, кроме букв, для проверки
    clean = "".join(filter(str.isalpha, text.lower()))
    return not any(word in clean for word in BAD_WORDS)

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
async def get_db_conn():
    return await asyncpg.connect(DB_URL)

async def check_user_limit(user_id):
    conn = await get_db_conn()
    row = await conn.fetchrow("SELECT last_post_date FROM users WHERE user_id = $1", user_id)
    if not row:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1)", user_id)
        await conn.close()
        return True, 0
    
    last_date = row['last_post_date']
    delta = datetime.datetime.now(datetime.timezone.utc) - last_date
    await conn.close()
    
    if delta.days < 7:
        return False, 7 - delta.days
    return True, 0

# --- ГЛАВНОЕ МЕНЮ ---
def main_menu_kb():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📢 Подать объявление")
    builder.button(text="🚨 ДТП / ЧП")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}! Это бот «Жизни Джигинки».\n"
        "Соблюдай правила: объявления 1 раз в неделю. Мат запрещен!",
        reply_markup=main_menu_kb()
    )

# --- СЦЕНАРИЙ ПОДАЧИ ОБЪЯВЛЕНИЯ ---
@dp.message(F.text == "📢 Подать объявление")
async def start_ad(message: types.Message, state: FSMContext):
    # Проверка лимита 7 дней
    allowed, days_left = await check_user_limit(message.from_user.id)
    if not allowed:
        return await message.answer(f"⏳ Рано! Правила группы: 1 раз в неделю.\nПодожди еще {days_left} дн.")
    
    await state.set_state(PostState.waiting_text)
    await message.answer("📝 Напиши текст объявления (описание, цена, контакты):")

@dp.message(PostState.waiting_text)
async def ad_text_input(message: types.Message, state: FSMContext):
    if not is_clean(message.text):
        return await message.answer("🤬 Ошибка! В тексте обнаружен мат. Перепиши нормально.")
    
    await state.update_data(text=message.text)
    await state.set_state(PostState.waiting_photo)
    await message.answer("📸 Отправь ОДНО фото (или нажми /skip если без фото):")

@dp.message(Command("skip"), PostState.waiting_photo)
@dp.message(PostState.waiting_photo, F.photo | F.text)
async def ad_photo_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photo_id = message.photo[-1].file_id if message.photo else None
    
    # Отправка админам на модерацию
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"ok_{message.from_user.id}")
    builder.button(text="❌ Отклонить", callback_data=f"no_{message.from_user.id}")
    
    msg_text = f"📥 **НОВОЕ ОБЪЯВЛЕНИЕ**\nОт: @{message.from_user.username}\n\n{data['text']}"
    
    if photo_id:
        await bot.send_photo(ADMIN_GROUP_ID, photo_id, caption=msg_text, reply_markup=builder.as_markup())
    else:
        await bot.send_message(ADMIN_GROUP_ID, msg_text, reply_markup=builder.as_markup())
    
    await message.answer("🤝 Принято! Отправил админам. Если всё ок — оно появится в канале.", reply_markup=main_menu_kb())
    await state.clear()

# --- СЦЕНАРИЙ ДТП / ЧП (БЕЗ ЛИМИТОВ) ---
@dp.message(F.text == "🚨 ДТП / ЧП")
async def chp_start(message: types.Message):
    await message.answer("⚠️ Если случилось что-то важное, просто пришли фото/видео и описание ОДНИМ сообщением. Мы опубликуем это максимально быстро!")

# --- ОБРАБОТКА КНОПОК АДМИНА ---
@dp.callback_query(F.data.startswith("ok_"))
async def approve_post(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    # Пересылаем в канал
    if callback.message.photo:
        await bot.send_photo(CHANNEL_ID, callback.message.photo[-1].file_id, caption=callback.message.caption)
    else:
        await bot.send_message(CHANNEL_ID, callback.message.text.replace("📥 НОВОЕ ОБЪЯВЛЕНИЕ\nОт:", "📢 ОБЪЯВЛЕНИЕ\nАвтор:"))
    
    # Обновляем дату в базе
    conn = await get_db_conn()
    await conn.execute("UPDATE users SET last_post_date = NOW() WHERE user_id = $1", user_id)
    await conn.close()
    
    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n✅ ОПУБЛИКОВАНО")
    await bot.send_message(user_id, "🎉 Твое объявление одобрено и опубликовано!")

@dp.callback_query(F.data.startswith("no_"))
async def reject_post(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n❌ ОТКЛОНЕНО")
    await bot.send_message(user_id, "😔 Админ отклонил твое объявление. Проверь правила (мат, частота постов).")

# --- ЗАПУСК ---
async def main():
    print("🚀 Бот Джигинки запущен на Python 3.12!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())