import asyncio
import re
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Подключаем Mistral и загрузчик окружения
from mistralai.client.sdk import Mistral
from dotenv import load_dotenv

# Загружаем переменные из файла .env
load_dotenv()

# Достаем токены
# Поддерживаем несколько вариантов имён переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("BOTTOKEN") or os.getenv("TOKEN")
MISTRAL_API_KEY = os.getenv("AI_TOKEN") or os.getenv("MISTRAL_API_KEY") or os.getenv("MISTRAL_KEY")

# Проверка на наличие токенов
if not BOT_TOKEN or not MISTRAL_API_KEY:
    print("Ошибка: не найден один или оба токена (BOT_TOKEN, MISTRAL_API_KEY). Проверь .env или переменные окружения.")
    raise SystemExit(1)

# Инициализация бота, диспетчера и планировщика
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Инициализация клиента Mistral
print("Инициализация клиента Mistral...")
mistral_client = Mistral(api_key=MISTRAL_API_KEY)
print("Mistral клиент инициализирован.")

# Машина состояний для пошагового опроса
class SetupForm(StatesGroup):
    people = State()
    sheets_per_visit = State()
    visits_per_day = State()
    purchase_date = State()
    brand = State()
    roll_count = State()

# --- 1. Старт и кнопка настройки ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(
        text="Давай настроим бота",
        callback_data="start_setup"
    ))
    await message.answer(
        "Привет! Я твой туалетный сомелье и бухгалтер. Я посчитаю, когда у тебя закончится бумага, и напомню купить новую.",
        reply_markup=builder.as_markup()
    )

# --- 2. Сбор данных (FSM) ---
@dp.callback_query(F.data == "start_setup")
async def start_setup(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Сколько людей пользуется бумагой? (Введи число)")
    await state.set_state(SetupForm.people)
    await callback.answer()

@dp.message(SetupForm.people)
async def process_people(message: types.Message, state: FSMContext):
    try:
        people_count = int(message.text)
        await state.update_data(people=people_count)
        await message.answer("Сколько листиков в среднем отрывает человек за один раз?")
        await state.set_state(SetupForm.sheets_per_visit)
    except ValueError:
        await message.answer("Пожалуйста, введи просто число.")

@dp.message(SetupForm.sheets_per_visit)
async def process_sheets(message: types.Message, state: FSMContext):
    try:
        sheets_count = int(message.text)
        await state.update_data(sheets_per_visit=sheets_count)
        await message.answer("Как часто (в среднем) один человек ходит в туалет за день?")
        await state.set_state(SetupForm.visits_per_day)
    except ValueError:
        await message.answer("Пожалуйста, введи просто число.")

@dp.message(SetupForm.visits_per_day)
async def process_visits(message: types.Message, state: FSMContext):
    try:
        visits_count = int(message.text)
        await state.update_data(visits_per_day=visits_count)
        await message.answer("Какого числа ты купил(а) упаковку? (введи в формате ДД.ММ.ГГГГ, например 25.10.2023)")
        await state.set_state(SetupForm.purchase_date)
    except ValueError:
        await message.answer("Пожалуйста, введи просто число.")

@dp.message(SetupForm.purchase_date)
async def process_date(message: types.Message, state: FSMContext):
    try:
        date_obj = datetime.strptime(message.text, "%d.%m.%Y")
        await state.update_data(purchase_date=date_obj)
        await message.answer("Какой бренд бумаги ты используешь? (Например: Zewa Deluxe, Papia, Familia)")
        await state.set_state(SetupForm.brand)
    except ValueError:
        await message.answer("Пожалуйста, введи дату в правильном формате: ДД.ММ.ГГГГ")

@dp.message(SetupForm.brand)
async def process_brand(message: types.Message, state: FSMContext):
    await state.update_data(brand=message.text)
    await message.answer("Сколько рулонов было в купленной упаковке? (Введи число, например 4, 8 или 12)")
    await state.set_state(SetupForm.roll_count)

# --- 3. Функция запроса к Mistral AI ---
async def get_sheets_from_ai(brand_name: str) -> int:
    prompt = f"Напиши только одно число. Какое среднее количество отрывных листов в одном стандартном рулоне туалетной бумаги бренда '{brand_name}'? Никаких других слов, только цифра."
    
    try:
        # Асинхронный запрос к Mistral (используем быструю модель)
        response = await mistral_client.chat.complete_async(
            model="mistral-small-latest",
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        
        # Получаем текст ответа
        answer_text = response.choices[0].message.content
        
        # Ищем цифры в ответе
        numbers = re.findall(r'\d+', answer_text)
        if numbers:
            return int(numbers[0])
        else:
            return 150 # Значение по умолчанию
            
    except Exception as e:
        print(f"Ошибка при запросе к Mistral: {e}")
        return 150 # Запасное значение при сбое сети

# --- 4. Финальный расчет и запуск таймера ---
@dp.message(SetupForm.roll_count)
async def process_roll_count_and_calculate(message: types.Message, state: FSMContext):
    try:
        roll_count = int(message.text)
    except ValueError:
        await message.answer("Пожалуйста, введи число.")
        return

    data = await state.get_data()
    brand = data['brand']
    people = data['people']
    sheets = data['sheets_per_visit']
    visits = data['visits_per_day']
    purchase_date = data['purchase_date']
    
    await message.answer(f"Ищу данные о рулонах {brand} через ИИ, подожди пару секунд... 🤖🔍")
    
    # Получаем данные от нейросети Mistral
    total_sheets_in_roll = await get_sheets_from_ai(brand)
    
    # Математика
    total_sheets_in_pack = total_sheets_in_roll * roll_count
    daily_usage = people * visits * sheets
    
    # Защита от деления на ноль
    if daily_usage == 0:
        await message.answer("Расход равен нулю. Кажется, эта бумага прослужит вечно!")
        await state.clear()
        return

    days_will_last = total_sheets_in_pack / daily_usage
    
    end_date = purchase_date + timedelta(days=days_will_last)
    reminder_date = end_date - timedelta(days=1)
    
    days_will_last_rounded = round(days_will_last)
    
    await message.answer(
        f"✅ <b>Успешно подсчитано!</b>\n\n"
        f"🧻 Бренд: {brand} (~{total_sheets_in_roll} листов в 1 рулоне)\n"
        f"📦 Куплено рулонов: {roll_count} (Всего ~{total_sheets_in_pack} листов)\n"
        f"📉 Ежедневный расход: {daily_usage} листов\n"
        f"🗓 Упаковки хватит примерно на: {days_will_last_rounded} дней.\n"
        f"🛑 Она закончится: {end_date.strftime('%d.%m.%Y')}\n\n"
        f"Я пришлю тебе напоминание за 1 день ({reminder_date.strftime('%d.%m.%Y')}), чтобы ты успел(а) в магазин!",
        parse_mode="HTML"
    )
    
    scheduler.add_job(
        send_reminder,
        trigger='date',
        run_date=reminder_date, 
        args=[message.chat.id]
    )
    
    await state.clear()

# --- 5. Функция напоминания ---
async def send_reminder(chat_id: int):
    await bot.send_message(chat_id, "🚨 Внимание! Твои запасы туалетной бумаги иссякнут через 1 день! Пора пополнить стратегический запас.")

# --- 6. Запуск бота ---
async def main():
    scheduler.start()
    print("Бот запущен и готов к работе! (scheduler запущен)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())