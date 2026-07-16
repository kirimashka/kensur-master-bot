# KENSUR_Master_Bot 1.3 (адаптированный для Render)
import os
import logging
import re
import asyncio
import json
import calendar
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from httpx import ConnectError, TimeoutException
from requests.exceptions import ConnectionError as RequestsConnectionError

# ========== НАСТРОЙКИ ==========
BOT_VERSION = "KENSUR_Master_Bot 1.3 (Render)"
TOKEN = "8714306378:AAEcPtbIQflVdP3gRwJSujqe2ujB7y5NZ1w"          # ← ваш токен
ADMIN_CHAT_ID = 413964692          # ← ваш личный ID (будет добавлен в админы)
GOOGLE_SHEETS_CREDENTIALS = "credentials.json"
SHEET_NAME = "Masters_Reports"     # название вашей таблицы
SHEET_ID = "1rwB81gYU-NeLQix9mj89Q7jxL8TxMnArSrSalA2B8hU"  # ID таблицы Masters_Reports (открытие по ID быстрее, чем поиск по имени)

# Настройка логирования (должна быть до первого использования logger)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Если на Render переданы credentials как переменная окружения, создаём файл
if "GOOGLE_CREDENTIALS" in os.environ:
    with open(GOOGLE_SHEETS_CREDENTIALS, "w") as f:
        f.write(os.environ["GOOGLE_CREDENTIALS"])
    logger.info("Файл credentials.json создан из переменной окружения.")

# Состояния для регистрации
(LAST_NAME, FIRST_NAME, MIDDLE_NAME, CITY, PHONE, BANK, SBP_PHONE, FIO_SBP) = range(8)

# Состояния для изменения профиля
(EDIT_CHOICE, EDIT_SBP_PHONE, EDIT_FIO_SBP, EDIT_CONFIRM) = range(8, 12)

# Состояния для отчета
(ADDR_CITY, ADDR_CITY_CONFIRM, ADDR_STREET, ADDR_STREET_CONFIRM,
 ADDR_HOUSE, ADDR_HOUSE_CONFIRM, ADDR_APARTMENT, ADDR_APARTMENT_CONFIRM,
 PHOTOS, EXTRA_EXPENSES, EXTRA_EXPENSES_CONFIRM) = range(12, 23)

# Состояния для выбора месяца в статистике (не используются как ConversationHandler, но оставим для совместимости)
(STATS_MONTH, STATS_YEAR) = range(23, 25)

# Состояния для администратора при оплате (не используются как ConversationHandler)
(AWAIT_PAYMENT_AMOUNT, AWAIT_PAYMENT_CONFIRM) = range(25, 27)

# Состояния для редактирования уже отправленного отчёта (до просмотра админом)
(EDITREPORT_CHOICE, EDITREPORT_ADDRESS, EDITREPORT_EXPENSES,
 EDITREPORT_PHOTOS, EDITREPORT_CONFIRM) = range(30, 35)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== ДЕКОРАТОР ДЛЯ ПОВТОРНЫХ ПОПЫТОК ПРИ СБОЯХ СЕТИ ==========
def retry_on_network_error(func):
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectError, TimeoutException, RequestsConnectionError, ConnectionError, gspread.exceptions.APIError)),
        before_sleep=lambda retry_state: logger.warning(f"Повторная попытка {retry_state.attempt_number} для {func.__name__} из-за {retry_state.outcome.exception()}")
    )(func)

# ========== РАБОТА С GOOGLE SHEETS ==========
_spreadsheet_cache = {"spreadsheet": None}

def get_sheet():
    """Открывает таблицу один раз за время жизни процесса и переиспользует объект —
    раньше каждый вызов заново авторизовался и искал таблицу по имени через Drive API."""
    if _spreadsheet_cache["spreadsheet"] is None:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS, scopes=scope)
        client = gspread.authorize(creds)
        client.http_client.session.timeout = (30, 60)
        _spreadsheet_cache["spreadsheet"] = client.open_by_key(SHEET_ID)
    return _spreadsheet_cache["spreadsheet"]

# ---------- Работа с мастерами ----------
CACHE_TTL_SECONDS = 600  # 10 минут
_masters_cache = {"records": None, "ts": 0}

def _invalidate_masters_cache():
    _masters_cache["records"] = None

@retry_on_network_error
def _get_masters_records():
    """Весь лист Masters одним запросом, с TTL-кэшем — раньше каждая проверка
    мастера (регистрация/меню/каждый шаг отчёта) заново перечитывала весь лист."""
    now = time.time()
    if _masters_cache["records"] is None or now - _masters_cache["ts"] >= CACHE_TTL_SECONDS:
        sheet = get_sheet()
        masters_sheet = sheet.worksheet("Masters")
        _masters_cache["records"] = masters_sheet.get_all_records()
        _masters_cache["ts"] = now
    return _masters_cache["records"]

@retry_on_network_error
def save_master(user_id, last_name, first_name, middle_name, city, phone, bank, sbp_phone, fio_sbp):
    sheet = get_sheet()
    masters_sheet = sheet.worksheet("Masters")
    masters_sheet.append_row([
        str(user_id),
        last_name,
        first_name,
        middle_name,
        city,
        phone,
        bank,
        sbp_phone,
        fio_sbp,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ])
    _invalidate_masters_cache()

def master_exists(user_id):
    records = _get_masters_records()
    for record in records:
        if str(record.get('user_id')) == str(user_id):
            return True
    return False

def get_master_data(user_id):
    records = _get_masters_records()
    for record in records:
        if str(record.get('user_id')) == str(user_id):
            return record
    return None

@retry_on_network_error
def update_master_sbp(user_id, sbp_phone, fio_sbp):
    sheet = get_sheet()
    masters_sheet = sheet.worksheet("Masters")
    cell = masters_sheet.find(str(user_id))
    if cell:
        row = cell.row
        masters_sheet.update_cell(row, 8, sbp_phone)  # колонка H
        masters_sheet.update_cell(row, 9, fio_sbp)    # колонка I
        _invalidate_masters_cache()
        return True
    return False

# ---------- Статистика мастера ----------
@retry_on_network_error
def get_master_stats(user_id, month=None, year=None):
    """
    Возвращает количество оплаченных установок и сумму выплат (payment_amount) для мастера.
    Если month и year заданы, фильтрует по указанному месяцу (по полю submitted_at).
    Если заданы только год и месяц, то берётся весь месяц.
    Если не заданы – берётся текущий месяц с начала месяца по сегодня.
    """
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    records = reports_sheet.get_all_records()
    count = 0
    total = 0.0
    now = datetime.now()
    if month is None or year is None:
        # По умолчанию текущий месяц с начала месяца по сегодня
        target_month = now.month
        target_year = now.year
        start_date = datetime(target_year, target_month, 1)
        end_date = now
    else:
        target_month = month
        target_year = year
        start_date = datetime(target_year, target_month, 1)
        # последний день месяца
        last_day = calendar.monthrange(target_year, target_month)[1]
        end_date = datetime(target_year, target_month, last_day, 23, 59, 59)

    for rec in records:
        if str(rec.get('user_id')) != str(user_id) or rec.get('payment_status') != 'оплачено':
            continue
        try:
            rec_date = datetime.strptime(rec.get('submitted_at'), "%Y-%m-%d %H:%M:%S")
            if rec_date < start_date or rec_date > end_date:
                continue
        except Exception as e:
            logger.warning(f"Ошибка парсинга даты в get_master_stats: {e}")
            continue
        count += 1
        try:
            total += float(rec.get('payment_amount', 0))
        except:
            pass
    return count, total

# ---------- Статистика по всем мастерам (для администратора) ----------
@retry_on_network_error
def get_all_masters_stats(month=None, year=None):
    """
    Возвращает словарь: {user_id: (fio, count, total)} для всех мастеров, у которых есть оплаченные установки.
    Если month и year заданы, фильтрует по указанному месяцу.
    Если не заданы – текущий месяц.
    """
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    masters_sheet = sheet.worksheet("Masters")
    records = reports_sheet.get_all_records()
    masters_records = masters_sheet.get_all_records()
    # Словарь для быстрого получения ФИО по user_id
    fio_dict = {}
    for m in masters_records:
        uid = str(m.get('user_id'))
        fio = f"{m.get('last_name', '')} {m.get('first_name', '')} {m.get('middle_name', '')}".strip()
        fio_dict[uid] = fio if fio else "Неизвестный"

    now = datetime.now()
    if month is None or year is None:
        target_month = now.month
        target_year = now.year
        start_date = datetime(target_year, target_month, 1)
        end_date = now
    else:
        target_month = month
        target_year = year
        start_date = datetime(target_year, target_month, 1)
        last_day = calendar.monthrange(target_year, target_month)[1]
        end_date = datetime(target_year, target_month, last_day, 23, 59, 59)

    stats = {}
    for rec in records:
        if rec.get('payment_status') != 'оплачено':
            continue
        try:
            rec_date = datetime.strptime(rec.get('submitted_at'), "%Y-%m-%d %H:%M:%S")
            if rec_date < start_date or rec_date > end_date:
                continue
        except Exception as e:
            logger.warning(f"Ошибка парсинга даты в get_all_masters_stats: {e}")
            continue
        uid = str(rec.get('user_id'))
        if uid not in stats:
            stats[uid] = {'count': 0, 'total': 0.0, 'fio': fio_dict.get(uid, 'Неизвестный')}
        stats[uid]['count'] += 1
        try:
            stats[uid]['total'] += float(rec.get('payment_amount', 0))
        except:
            pass
    return stats

# ---------- Получение списка месяцев, в которых есть отчёты ----------
@retry_on_network_error
def get_months_with_reports():
    """Возвращает список кортежей (год, месяц) для месяцев, в которых есть хотя бы один оплаченный отчёт."""
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    records = reports_sheet.get_all_records()
    months_set = set()
    for rec in records:
        if rec.get('payment_status') != 'оплачено':
            continue
        try:
            rec_date = datetime.strptime(rec.get('submitted_at'), "%Y-%m-%d %H:%M:%S")
            months_set.add((rec_date.year, rec_date.month))
        except Exception as e:
            logger.warning(f"Ошибка парсинга даты в get_months_with_reports: {e}")
            continue
    # Сортируем по убыванию (сначала новые)
    return sorted(months_set, reverse=True)

# ---------- Работа с отчетами ----------
_reports_header_ensured = {"done": False}

def _ensure_admin_viewed_header(reports_sheet):
    """Колонка P (admin_viewed) появилась в новой версии бота — если в существующей
    таблице заголовка ещё нет, дописываем его один раз за время жизни процесса."""
    if _reports_header_ensured["done"]:
        return
    if not reports_sheet.acell('P1').value:
        reports_sheet.update_acell('P1', 'admin_viewed')
    _reports_header_ensured["done"] = True

@retry_on_network_error
def save_report(user_id, photos, extra_expenses, last_name, first_name, middle_name,
                addr_city, addr_street, addr_house, addr_apartment):
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    _ensure_admin_viewed_header(reports_sheet)
    report_id = f"{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    photos_str = ",".join(photos) if photos else ""
    reports_sheet.append_row([
        report_id,
        str(user_id),
        photos_str,
        extra_expenses,
        "",  # payment_amount
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "не оплачено",
        last_name,
        first_name,
        middle_name,
        addr_city,
        addr_street,
        addr_house,
        addr_apartment,
        "не подтверждено",
        ""  # admin_viewed
    ])
    return report_id

@retry_on_network_error
def update_report_payment_amount(report_id, amount):
    """Сохраняет сумму оплаты в отчёт (без изменения статуса)."""
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    cell = reports_sheet.find(report_id)
    if cell:
        row = cell.row
        reports_sheet.update_cell(row, 5, amount)  # payment_amount
        return True
    return False

@retry_on_network_error
def mark_report_paid(report_id):
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    cell = reports_sheet.find(report_id)
    if cell:
        reports_sheet.update_cell(cell.row, 7, "оплачено") # payment_status
        return True
    return False

@retry_on_network_error
def mark_master_confirmed(report_id):
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    cell = reports_sheet.find(report_id)
    if cell:
        reports_sheet.update_cell(cell.row, 15, "подтверждено") # master_confirmed
        return True
    return False

@retry_on_network_error
def mark_report_viewed(report_id):
    """Отмечает, что админ открыл отчёт — до этого момента мастер может его
    редактировать или отменить (см. get_master_reports/update_report_fields)."""
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    _ensure_admin_viewed_header(reports_sheet)
    cell = reports_sheet.find(report_id)
    if cell:
        reports_sheet.update_cell(cell.row, 16, "просмотрено") # admin_viewed
        return True
    return False

@retry_on_network_error
def update_report_fields(report_id, photos=None, extra_expenses=None,
                          addr_city=None, addr_street=None, addr_house=None, addr_apartment=None):
    """Точечно переписывает адрес/фото/расходы уже отправленного отчёта, не трогая
    payment_amount/payment_status/submitted_at/master_confirmed/admin_viewed."""
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    cell = reports_sheet.find(report_id)
    if not cell:
        return False
    row = cell.row
    if photos is not None or extra_expenses is not None:
        current = reports_sheet.row_values(row)
        photos_val = ",".join(photos) if photos is not None else (current[2] if len(current) > 2 else "")
        expenses_val = extra_expenses if extra_expenses is not None else (current[3] if len(current) > 3 else "")
        reports_sheet.update(f'C{row}:D{row}', [[photos_val, expenses_val]])
    if addr_city is not None or addr_street is not None or addr_house is not None or addr_apartment is not None:
        current = reports_sheet.row_values(row)
        def _keep(new_val, idx):
            return new_val if new_val is not None else (current[idx] if len(current) > idx else "")
        reports_sheet.update(f'K{row}:N{row}', [[
            _keep(addr_city, 10), _keep(addr_street, 11), _keep(addr_house, 12), _keep(addr_apartment, 13)
        ]])
    return True

@retry_on_network_error
def delete_report_row(report_id):
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    cell = reports_sheet.find(report_id)
    if cell:
        reports_sheet.delete_rows(cell.row)
        return True
    return False

@retry_on_network_error
def get_master_reports(user_id, limit=10):
    """Последние отчёты мастера (любого статуса оплаты), новые сверху — для «Мои отчёты»."""
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    records = reports_sheet.get_all_records()
    mine = [rec for rec in records if str(rec.get('user_id')) == str(user_id)]
    def _sort_key(rec):
        try:
            return datetime.strptime(rec.get('submitted_at', ''), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.min
    mine.sort(key=_sort_key, reverse=True)
    return mine[:limit]

@retry_on_network_error
def get_report_by_id(report_id):
    sheet = get_sheet()
    reports_sheet = sheet.worksheet("Reports")
    records = reports_sheet.get_all_records()
    for rec in records:
        if rec.get('report_id') == report_id:
            return rec
    return None

# ---------- Работа с черновиками ----------
@retry_on_network_error
def save_draft(user_id, step, addr_city=None, addr_street=None, addr_house=None,
               addr_apartment=None, photos=None, extra_expenses=None):
    """Сохраняет или обновляет черновик отчёта для пользователя."""
    sheet = get_sheet()
    try:
        drafts_sheet = sheet.worksheet("Drafts")
    except gspread.WorksheetNotFound:
        drafts_sheet = sheet.add_worksheet(title="Drafts", rows="100", cols=9)
        drafts_sheet.update('A1:I1', [['user_id', 'step', 'addr_city', 'addr_street',
                                        'addr_house', 'addr_apartment', 'photos',
                                        'extra_expenses', 'updated_at']])
    try:
        cell = drafts_sheet.find(str(user_id))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Подготавливаем значения, гарантируем, что они строки и не None
        step_str = str(step) if step is not None else ""
        addr_city_str = str(addr_city) if addr_city is not None else ""
        addr_street_str = str(addr_street) if addr_street is not None else ""
        addr_house_str = str(addr_house) if addr_house is not None else ""
        addr_apartment_str = str(addr_apartment) if addr_apartment is not None else ""
        photos_str = ",".join(photos) if photos else ""
        extra_expenses_str = str(extra_expenses) if extra_expenses is not None else ""

        if cell:
            row = cell.row
            # Одним запросом обновляем весь диапазон B:I вместо 6 отдельных update_acell
            drafts_sheet.update(f'B{row}:I{row}', [[
                step_str, addr_city_str, addr_street_str, addr_house_str,
                addr_apartment_str, photos_str, extra_expenses_str, now
            ]])
        else:
            new_row = [
                str(user_id),
                step_str,
                addr_city_str,
                addr_street_str,
                addr_house_str,
                addr_apartment_str,
                photos_str,
                extra_expenses_str,
                now
            ]
            drafts_sheet.append_row(new_row)
    except Exception as e:
        logger.error(f"Ошибка при сохранении черновика: {e}")
        # Не перевыбрасываем, чтобы не ломать бота

@retry_on_network_error
def get_draft(user_id):
    sheet = get_sheet()
    try:
        drafts_sheet = sheet.worksheet("Drafts")
    except:
        return None
    try:
        cell = drafts_sheet.find(str(user_id))
        if cell:
            row = drafts_sheet.row_values(cell.row)
            return {
                'user_id': row[0],
                'step': int(row[1]) if row[1] else None,
                'addr_city': row[2],
                'addr_street': row[3],
                'addr_house': row[4],
                'addr_apartment': row[5],
                'photos': row[6].split(',') if row[6] else [],
                'extra_expenses': float(row[7]) if row[7] else None,
                'updated_at': row[8]
            }
    except:
        pass
    return None

@retry_on_network_error
def delete_draft(user_id):
    sheet = get_sheet()
    try:
        drafts_sheet = sheet.worksheet("Drafts")
        cell = drafts_sheet.find(str(user_id))
        if cell:
            drafts_sheet.delete_rows(cell.row)
    except Exception as e:
        logger.error(f"Ошибка при удалении черновика: {e}")

# ---------- Работа с администраторами ----------
_admins_cache = {"ids": None, "ts": 0}

@retry_on_network_error
def get_admins():
    """Список админов почти никогда не меняется, но раньше перечитывался с нуля
    на каждой проверке is_admin() — а таких проверок ~20 на файл. TTL-кэш 10 минут."""
    now = time.time()
    if _admins_cache["ids"] is not None and now - _admins_cache["ts"] < CACHE_TTL_SECONDS:
        return _admins_cache["ids"]

    sheet = get_sheet()
    try:
        admins_sheet = sheet.worksheet("Admins")
    except gspread.WorksheetNotFound:
        admins_sheet = sheet.add_worksheet(title="Admins", rows="100", cols="1")
        admins_sheet.update_cell(1, 1, "admin_id")
        admins_sheet.append_row([str(ADMIN_CHAT_ID)])
        _admins_cache["ids"] = [str(ADMIN_CHAT_ID)]
        _admins_cache["ts"] = now
        return _admins_cache["ids"]

    records = admins_sheet.get_all_records()
    admin_ids = [str(record.get('admin_id')) for record in records if record.get('admin_id')]
    if not admin_ids:
        admins_sheet.append_row([str(ADMIN_CHAT_ID)])
        admin_ids = [str(ADMIN_CHAT_ID)]
    _admins_cache["ids"] = admin_ids
    _admins_cache["ts"] = now
    return admin_ids

def is_admin(user_id):
    return str(user_id) in get_admins()

# ========== ПРОВЕРКА И ФОРМАТИРОВАНИЕ ТЕЛЕФОНА ==========
def is_valid_phone(phone):
    cleaned = re.sub(r'[\s\-\(\)]', '', str(phone))
    pattern = r'^(\+7|8)\d{10}$'
    return re.match(pattern, cleaned) is not None

def format_phone(phone):
    phone = str(phone)
    cleaned = re.sub(r'[\s\-\(\)]', '', phone)
    if cleaned.startswith('8') and len(cleaned) == 11:
        return '+7' + cleaned[1:]
    elif cleaned.startswith('7') and len(cleaned) == 11:
        return '+' + cleaned
    elif cleaned.startswith('+7') and len(cleaned) == 12:
        return cleaned
    else:
        return phone

# ========== ГЛАВНОЕ МЕНЮ ДЛЯ МАСТЕРА ==========
def get_main_menu(is_admin_user=False):
    if is_admin_user:
        keyboard = [
            [KeyboardButton("📸 Новая установка")],
            [KeyboardButton("📋 Мои отчёты")],
            [KeyboardButton("📊 Статистика")],
            [KeyboardButton("📊 Результат мастеров")],
            [KeyboardButton("✏️ Изменить СБП-реквизиты")]
        ]
    else:
        keyboard = [
            [KeyboardButton("📸 Новая установка")],
            [KeyboardButton("📋 Мои отчёты")],
            [KeyboardButton("📊 Статистика")],
            [KeyboardButton("✏️ Изменить СБП-реквизиты")]
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin_user = is_admin(user_id)
    await update.message.reply_text(
        "Выберите действие:",
        reply_markup=get_main_menu(is_admin_user)
    )

# ========== РЕГИСТРАЦИЯ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        if master_exists(user_id):
            await show_main_menu(update, context)
            return ConversationHandler.END
        await update.message.reply_text(
            "Добро пожаловать! Давайте зарегистрируемся.\n"
            "Введите вашу фамилию:"
        )
        return LAST_NAME
    except Exception as e:
        logger.error(f"Ошибка в start: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуйте позже.")
        return ConversationHandler.END

async def last_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['last_name'] = update.message.text.strip()
    await update.message.reply_text("Введите ваше имя:")
    return FIRST_NAME

async def first_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['first_name'] = update.message.text.strip()
    await update.message.reply_text("Введите ваше отчество (если нет, введите '-'):")
    return MIDDLE_NAME

async def middle_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['middle_name'] = update.message.text.strip()
    await update.message.reply_text("Введите ваш город:")
    return CITY

async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['city'] = update.message.text.strip()
    await update.message.reply_text("Введите ваш номер телефона для связи (например, +79991234567 или 89991234567):")
    return PHONE

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if not is_valid_phone(phone):
        await update.message.reply_text("Некорректный формат. Введите номер в формате +7XXXXXXXXXX или 8XXXXXXXXXX:")
        return PHONE
    context.user_data['phone'] = phone
    await update.message.reply_text("Введите название вашего банка (для перевода по СБП):")
    return BANK

async def bank_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['bank'] = update.message.text.strip()
    await update.message.reply_text("Введите номер телефона для перевода по СБП (в любом формате):")
    return SBP_PHONE

async def sbp_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['sbp_phone'] = update.message.text.strip()
    await update.message.reply_text("Введите ФИО получателя по СБП (как в банке):")
    return FIO_SBP

async def fio_sbp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    context.user_data['fio_sbp'] = update.message.text.strip()
    try:
        save_master(
            user_id,
            context.user_data['last_name'],
            context.user_data['first_name'],
            context.user_data['middle_name'],
            context.user_data['city'],
            context.user_data['phone'],
            context.user_data['bank'],
            context.user_data['sbp_phone'],
            context.user_data['fio_sbp']
        )
        await update.message.reply_text(
            "Регистрация завершена!",
            reply_markup=get_main_menu(is_admin(user_id))
        )
    except Exception as e:
        logger.error(f"Ошибка сохранения мастера: {e}")
        await update.message.reply_text("Ошибка при сохранении. Попробуйте позже.")
    return ConversationHandler.END

# ========== ИЗМЕНЕНИЕ СБП-РЕКВИЗИТОВ ==========
async def edit_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not master_exists(user_id):
        await update.message.reply_text("Сначала зарегистрируйтесь через /start")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("📱 Изменить телефон СБП", callback_data="edit_sbp_phone")],
        [InlineKeyboardButton("👤 Изменить ФИО получателя", callback_data="edit_fio_sbp")],
        [InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Что вы хотите изменить?", reply_markup=reply_markup)
    return EDIT_CHOICE

async def edit_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    if data == "edit_sbp_phone":
        await safe_edit_message(query, "Введите новый номер телефона для СБП:", None)
        context.user_data['edit_field'] = 'sbp_phone'
        return EDIT_SBP_PHONE
    elif data == "edit_fio_sbp":
        await safe_edit_message(query, "Введите новое ФИО получателя:", None)
        context.user_data['edit_field'] = 'fio_sbp'
        return EDIT_FIO_SBP
    elif data == "edit_cancel":
        await safe_edit_message(query, "Изменение отменено.", None)
        return ConversationHandler.END

async def edit_sbp_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_value = update.message.text.strip()
    context.user_data['new_sbp_phone'] = new_value
    await update.message.reply_text(f"Новый телефон: {new_value}\nВсё верно?", reply_markup=yes_no_keyboard())
    return EDIT_CONFIRM

async def edit_fio_sbp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_value = update.message.text.strip()
    context.user_data['new_fio_sbp'] = new_value
    await update.message.reply_text(f"Новое ФИО: {new_value}\nВсё верно?", reply_markup=yes_no_keyboard())
    return EDIT_CONFIRM

async def edit_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.lower()
    if text == 'да':
        field = context.user_data.get('edit_field')
        if field == 'sbp_phone':
            new_phone = context.user_data['new_sbp_phone']
            master = get_master_data(user_id)
            if master:
                update_master_sbp(user_id, new_phone, master['fio_sbp'])
                await update.message.reply_text("Телефон СБП обновлён!", reply_markup=get_main_menu(is_admin(user_id)))
        elif field == 'fio_sbp':
            new_fio = context.user_data['new_fio_sbp']
            master = get_master_data(user_id)
            if master:
                update_master_sbp(user_id, master['sbp_phone'], new_fio)
                await update.message.reply_text("ФИО получателя обновлено!", reply_markup=get_main_menu(is_admin(user_id)))
        return ConversationHandler.END
    else:
        await update.message.reply_text("Изменение отменено.", reply_markup=get_main_menu(is_admin(user_id)))
        return ConversationHandler.END

# ========== РЕДАКТИРОВАНИЕ УЖЕ ОТПРАВЛЕННОГО ОТЧЁТА (до просмотра админом) ==========
async def edit_report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    report_id = query.data.replace("editreport_", "")
    user_id = update.effective_user.id
    report = get_report_by_id(report_id)
    if not report or str(report.get('user_id')) != str(user_id):
        await safe_edit_message(query, "Отчёт не найден.", None)
        return ConversationHandler.END
    if report.get('payment_status') == 'оплачено' or report.get('admin_viewed'):
        await safe_edit_message(query, "Отчёт уже просмотрен администратором или оплачен — изменить нельзя.", None)
        return ConversationHandler.END

    context.user_data['edit_report_id'] = report_id
    keyboard = [
        [InlineKeyboardButton("📍 Адрес", callback_data="er_address")],
        [InlineKeyboardButton("💰 Доп. расходы", callback_data="er_expenses")],
        [InlineKeyboardButton("📸 Фото", callback_data="er_photos")],
        [InlineKeyboardButton("❌ Отмена", callback_data="er_cancel")]
    ]
    await safe_edit_message(query, "Что хотите изменить в отчёте?", InlineKeyboardMarkup(keyboard))
    return EDITREPORT_CHOICE

async def edit_report_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "er_address":
        context.user_data['er_edit_kind'] = 'address'
        context.user_data['er_addr_step'] = 0
        await safe_edit_message(query, "Введите новый город:", None)
        return EDITREPORT_ADDRESS
    elif data == "er_expenses":
        context.user_data['er_edit_kind'] = 'expenses'
        await safe_edit_message(query, "Введите новую сумму доп. расходов (в рублях, неотрицательное число):", None)
        return EDITREPORT_EXPENSES
    elif data == "er_photos":
        context.user_data['er_edit_kind'] = 'photos'
        context.user_data['er_photos'] = []
        await safe_edit_message(
            query,
            "Пришлите новые фото взамен старых — по одному, до 5 штук, затем напишите 'готово':",
            None
        )
        return EDITREPORT_PHOTOS
    else:  # er_cancel
        await safe_edit_message(query, "Изменение отменено.", None)
        context.user_data.pop('edit_report_id', None)
        return ConversationHandler.END

async def edit_report_address_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    step = context.user_data.get('er_addr_step', 0)
    value = update.message.text.strip()
    fields = ['er_city', 'er_street', 'er_house', 'er_apartment']
    context.user_data[fields[step]] = value
    step += 1
    if step < 4:
        context.user_data['er_addr_step'] = step
        prompts = ["Введите улицу:", "Введите номер дома:", "Введите номер квартиры/офиса:"]
        await update.message.reply_text(prompts[step - 1])
        return EDITREPORT_ADDRESS
    context.user_data.pop('er_addr_step', None)
    summary = (
        f"Новый адрес: {context.user_data['er_city']}, {context.user_data['er_street']}, "
        f"д.{context.user_data['er_house']}, кв.{context.user_data['er_apartment']}\nВсё верно?"
    )
    await update.message.reply_text(summary, reply_markup=yes_no_keyboard())
    return EDITREPORT_CONFIRM

async def edit_report_expenses_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = float(update.message.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите неотрицательное число, например 500 или 0.")
        return EDITREPORT_EXPENSES
    context.user_data['er_expenses'] = value
    await update.message.reply_text(f"Новая сумма доп. расходов: {value} руб.\nВсё верно?", reply_markup=yes_no_keyboard())
    return EDITREPORT_CONFIRM

async def edit_report_photos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        text = (update.message.text or "").strip().lower()
        if text in ["готово", "/done"]:
            photos = context.user_data.get('er_photos', [])
            if not photos:
                await update.message.reply_text("Пришлите хотя бы одно фото.")
                return EDITREPORT_PHOTOS
            await update.message.reply_text(f"Новых фото: {len(photos)}. Всё верно?", reply_markup=yes_no_keyboard())
            return EDITREPORT_CONFIRM
        await update.message.reply_text("Пришлите фото (по одному) или напишите 'готово'.")
        return EDITREPORT_PHOTOS

    photos = context.user_data.setdefault('er_photos', [])
    if len(photos) >= 5:
        await update.message.reply_text("Уже 5 фото — это максимум. Напишите 'готово'.")
        return EDITREPORT_PHOTOS
    try:
        photo_file = await update.message.photo[-1].get_file()
    except Exception as e:
        logger.error(f"Ошибка скачивания фото при редактировании отчёта: {e}")
        await update.message.reply_text("Не удалось загрузить фото, попробуйте ещё раз.")
        return EDITREPORT_PHOTOS
    photos.append(photo_file.file_id)
    remaining = 5 - len(photos)
    await update.message.reply_text(f"Фото добавлено ({len(photos)}/5). Ещё {remaining} можно добавить, или напишите 'готово'.")
    return EDITREPORT_PHOTOS

async def edit_report_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    report_id = context.user_data.get('edit_report_id')
    kind = context.user_data.get('er_edit_kind')
    if query.data == "confirm_yes" and report_id and kind:
        try:
            if kind == 'address':
                update_report_fields(
                    report_id,
                    addr_city=context.user_data.get('er_city'),
                    addr_street=context.user_data.get('er_street'),
                    addr_house=context.user_data.get('er_house'),
                    addr_apartment=context.user_data.get('er_apartment')
                )
            elif kind == 'expenses':
                update_report_fields(report_id, extra_expenses=context.user_data.get('er_expenses'))
            elif kind == 'photos':
                update_report_fields(report_id, photos=context.user_data.get('er_photos', []))
            await safe_edit_message(query, "✅ Отчёт обновлён.", None)
        except Exception as e:
            logger.error(f"Ошибка обновления отчёта {report_id}: {e}")
            await safe_edit_message(query, "Не удалось обновить отчёт. Попробуйте позже.", None)
    else:
        await safe_edit_message(query, "Изменение отменено.", None)

    for key in ('edit_report_id', 'er_edit_kind', 'er_city', 'er_street', 'er_house',
                'er_apartment', 'er_expenses', 'er_photos', 'er_addr_step'):
        context.user_data.pop(key, None)
    user_id = update.effective_user.id
    await context.bot.send_message(chat_id=user_id, text="Выберите действие:", reply_markup=get_main_menu(is_admin(user_id)))
    return ConversationHandler.END

# ========== ОБРАБОТКА КНОПОК МЕНЮ ==========
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text
    logger.info(f"menu_handler: получил текст '{text}' от пользователя {user_id}")

    if not master_exists(user_id):
        await update.message.reply_text("Сначала зарегистрируйтесь через /start")
        return

    is_admin_user = is_admin(user_id)

    if text == "📸 Новая установка":
        return  # обработчик будет в report_conv
    elif text == "📊 Статистика":
        try:
            count, total = get_master_stats(user_id)  # за текущий месяц
            master = get_master_data(user_id)
            fio = f"{master['last_name']} {master['first_name']} {master['middle_name']}".strip()
            now = datetime.now()
            msg = (
                f"📊 Статистика для {fio} за {calendar.month_name[now.month]} {now.year} (с начала месяца по сегодня):\n"
                f"Количество оплаченных установок: {count}\n"
                f"Общая сумма выплат: {total:.2f} руб.\n\n"
                "Выберите месяц для просмотра статистики за другой период:"
            )
            # Список месяцев с числовыми индексами
            months = [
                ("Январь", 1), ("Февраль", 2), ("Март", 3),
                ("Апрель", 4), ("Май", 5), ("Июнь", 6),
                ("Июль", 7), ("Август", 8), ("Сентябрь", 9),
                ("Октябрь", 10), ("Ноябрь", 11), ("Декабрь", 12)
            ]
            # Разбиваем на строки по 3
            month_rows = [months[i:i+3] for i in range(0, len(months), 3)]
            keyboard = []
            for row in month_rows:
                buttons = []
                for month_name, month_num in row:
                    buttons.append(InlineKeyboardButton(month_name, callback_data=f"stats_master_month_{month_num}"))
                keyboard.append(buttons)
            keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="stats_close")])
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Ошибка в статистике для мастера {user_id}: {e}")
            await update.message.reply_text("Произошла ошибка при загрузке статистики.")
    elif text == "📊 Результат мастеров" and is_admin_user:
        try:
            await show_all_masters_stats(update, context)
        except Exception as e:
            logger.error(f"Ошибка в результате мастеров для админа {user_id}: {e}")
            await update.message.reply_text("Произошла ошибка при загрузке статистики мастеров.")
    elif text == "✏️ Изменить СБП-реквизиты":
        await edit_profile(update, context)
    elif text == "📋 Мои отчёты":
        await my_reports_handler(update, context)

# ========== ИСТОРИЯ ОТЧЁТОВ МАСТЕРА ==========
async def my_reports_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        reports = get_master_reports(user_id, limit=10)
    except Exception as e:
        logger.error(f"Ошибка в get_master_reports для {user_id}: {e}")
        await update.message.reply_text("Не удалось загрузить историю отчётов. Попробуйте позже.")
        return

    if not reports:
        await update.message.reply_text("У вас пока нет отправленных отчётов.")
        return

    keyboard = []
    for rec in reports:
        if rec.get('payment_status') == 'оплачено':
            status_emoji = "✅"
        elif rec.get('admin_viewed'):
            status_emoji = "👁"
        else:
            status_emoji = "🕓"
        date_part = (rec.get('submitted_at') or '')[:10]
        label = f"{status_emoji} {rec.get('address_city', '')}, {rec.get('address_street', '')} д.{rec.get('address_house', '')} — {date_part}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"myreport_{rec.get('report_id')}")])

    await update.message.reply_text(
        "📋 Ваши последние отчёты (✅ оплачен · 👁 на рассмотрении у админа · 🕓 ожидает):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ========== ПОКАЗ СТАТИСТИКИ ПО ВСЕМ МАСТЕРАМ (для администратора) ==========
async def show_all_masters_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, month=None, year=None):
    user_id = update.effective_user.id if isinstance(update, Update) else update
    if month is None or year is None:
        now = datetime.now()
        month = now.month
        year = now.year
    stats = get_all_masters_stats(month, year)
    if not stats:
        text = f"За {calendar.month_name[month]} {year} нет оплаченных установок."
    else:
        lines = [f"Статистика за {calendar.month_name[month]} {year}:"]
        for uid, data in stats.items():
            lines.append(f"{data['fio']}: {data['count']} уст., {data['total']:.2f} руб.")
        text = "\n".join(lines)

    # Получаем список месяцев с отчётами
    months_list = get_months_with_reports()
    keyboard = []
    # Создаём кнопки для каждого месяца
    for y, m in months_list:
        btn_text = f"{calendar.month_name[m]} {y}"
        callback = f"stats_admin_month_{y}_{m}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback)])
    # Добавляем кнопку закрытия
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="stats_close")])

    if isinstance(update, Update):
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        # Если вызвано из callback, нужно отредактировать сообщение
        query = context
        await safe_edit_message(query, text, InlineKeyboardMarkup(keyboard))

# ========== АВТОСВОДКА В НАЧАЛЕ МЕСЯЦА ==========
def _previous_month_year():
    now = datetime.now()
    if now.month == 1:
        return 12, now.year - 1
    return now.month - 1, now.year

async def send_monthly_summaries(context: ContextTypes.DEFAULT_TYPE):
    """Шлёт каждому мастеру личную сводку за прошлый месяц + дайджест админам.
    Вызывается автоматически 1-го числа (JobQueue) и вручную командой /monthly_summary_now."""
    month, year = _previous_month_year()
    month_name = calendar.month_name[month]

    masters = _get_masters_records()
    sent, failed = 0, 0
    for m in masters:
        uid = m.get('user_id')
        if not uid:
            continue
        try:
            count, total = get_master_stats(uid, month=month, year=year)
            fio = f"{m.get('last_name', '')} {m.get('first_name', '')} {m.get('middle_name', '')}".strip()
            text = (
                f"📊 Итоги за {month_name} {year} для {fio}:\n"
                f"Оплаченных установок: {count}\n"
                f"Общая сумма выплат: {total:.2f} руб."
            )
            await context.bot.send_message(chat_id=int(uid), text=text)
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"Не удалось отправить месячную сводку мастеру {uid}: {e}")

    try:
        stats = get_all_masters_stats(month, year)
        if stats:
            lines = [f"📊 Дайджест за {month_name} {year} (автосводка):"]
            for uid, data in stats.items():
                lines.append(f"{data['fio']}: {data['count']} уст., {data['total']:.2f} руб.")
            admin_text = "\n".join(lines)
        else:
            admin_text = f"📊 Автосводка: за {month_name} {year} нет оплаченных установок."
        for admin_id in get_admins():
            try:
                await context.bot.send_message(chat_id=int(admin_id), text=admin_text)
            except Exception as e:
                logger.error(f"Не удалось отправить дайджест админу {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Ошибка формирования дайджеста для админов: {e}")

    logger.info(f"Месячная автосводка за {month_name} {year}: отправлено {sent}, ошибок {failed}")

async def monthly_summary_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    await update.message.reply_text("Рассылаю сводку за прошлый месяц...")
    await send_monthly_summaries(context)
    await update.message.reply_text("Готово.")

# ========== ВЫБОР МЕСЯЦА ДЛЯ СТАТИСТИКИ МАСТЕРА ==========
async def stats_master_month_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    month = int(data.replace("stats_master_month_", ""))
    # Запрашиваем год (можно предложить выбор года, но для простоты возьмём текущий)
    now = datetime.now()
    year = now.year
    try:
        count, total = get_master_stats(user_id, month=month, year=year)
        master = get_master_data(user_id)
        fio = f"{master['last_name']} {master['first_name']} {master['middle_name']}".strip()
        msg = (
            f"📊 Статистика для {fio} за {calendar.month_name[month]} {year}:\n"
            f"Количество оплаченных установок: {count}\n"
            f"Общая сумма выплат: {total:.2f} руб."
        )
    except Exception as e:
        logger.error(f"Ошибка в stats_master_month_callback: {e}")
        msg = "Ошибка при загрузке статистики."
    # Клавиатура с месяцами и кнопкой закрыть
    months = [
        ("Январь", 1), ("Февраль", 2), ("Март", 3),
        ("Апрель", 4), ("Май", 5), ("Июнь", 6),
        ("Июль", 7), ("Август", 8), ("Сентябрь", 9),
        ("Октябрь", 10), ("Ноябрь", 11), ("Декабрь", 12)
    ]
    month_rows = [months[i:i+3] for i in range(0, len(months), 3)]
    keyboard = []
    for row in month_rows:
        buttons = []
        for month_name, month_num in row:
            buttons.append(InlineKeyboardButton(month_name, callback_data=f"stats_master_month_{month_num}"))
        keyboard.append(buttons)
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="stats_close")])
    await safe_edit_message(query, msg, InlineKeyboardMarkup(keyboard))

# ========== ВЫБОР МЕСЯЦА ДЛЯ СТАТИСТИКИ АДМИНИСТРАТОРА ==========
async def stats_admin_month_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.replace("stats_admin_month_", "").split("_")
    year = int(parts[0])
    month = int(parts[1])
    await show_all_masters_stats(update, context, month, year)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ КНОПОК ПОДТВЕРЖДЕНИЯ ==========
def yes_no_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Да", callback_data="confirm_yes"),
         InlineKeyboardButton("🔄 Изменить", callback_data="confirm_no")],
        [InlineKeyboardButton("❌ Отмена", callback_data="confirm_cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def confirm_payment_keyboard(report_id):
    keyboard = [
        [InlineKeyboardButton("✅ Отметить оплаченным", callback_data=f"pay_{report_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="confirm_cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def safe_edit_message(query, text, reply_markup=None):
    """Безопасное редактирование сообщения с fallback на отправку нового."""
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Не удалось отредактировать сообщение: {e}")
        if reply_markup:
            await query.message.reply_text(text=text, reply_markup=reply_markup)
        else:
            await query.message.reply_text(text=text)

REPORT_TOTAL_STEPS = 6  # город, улица, дом, квартира, фото, доп. расходы

def step_suffix(step_number: int) -> str:
    """step_number — номер шага, который сейчас начинает заполнять мастер (1..6)."""
    return f"\n\n📍 Шаг {step_number} из {REPORT_TOTAL_STEPS}"

# ========== СОЗДАНИЕ ОТЧЕТА ==========
async def new_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        if not master_exists(user_id):
            await update.message.reply_text("Сначала зарегистрируйтесь через /start")
            return ConversationHandler.END

        draft = get_draft(user_id)
        if draft:
            context.user_data['addr_city'] = draft.get('addr_city')
            context.user_data['addr_street'] = draft.get('addr_street')
            context.user_data['addr_house'] = draft.get('addr_house')
            context.user_data['addr_apartment'] = draft.get('addr_apartment')
            context.user_data['photos'] = draft.get('photos', [])
            context.user_data['extra_expenses'] = draft.get('extra_expenses')
            step = draft.get('step')
            if step == 0:
                await update.message.reply_text("Введите город установки:" + step_suffix(1))
                return ADDR_CITY
            elif step == 1:
                await update.message.reply_text("Введите улицу:" + step_suffix(2))
                return ADDR_STREET
            elif step == 2:
                await update.message.reply_text("Введите номер дома:" + step_suffix(3))
                return ADDR_HOUSE
            elif step == 3:
                await update.message.reply_text("Введите номер квартиры/офиса:" + step_suffix(4))
                return ADDR_APARTMENT
            elif step == 4:
                await update.message.reply_text(
                    "📸 Отправляйте фото установки (не более 5 штук).\n"
                    "Вы можете прислать фото снаружи, изнутри, с торца и ответную часть, и обязательно заполненного гарантийного талона.\n"
                    "После каждого фото я буду сообщать, сколько ещё можно добавить.\n"
                    "Когда загрузите все фото, отправьте команду /done или просто напишите 'готово'.\n"
                    "Чтобы убрать последнее загруженное фото, напишите 'удалить'."
                    + step_suffix(5)
                )
                return PHOTOS
            elif step == 5:
                await update.message.reply_text("Введите сумму дополнительных расходов (в рублях, неотрицательное число):" + step_suffix(6))
                return EXTRA_EXPENSES
            else:
                await update.message.reply_text("Ошибка в черновике. Начните заново.")
                delete_draft(user_id)
                context.user_data.clear()
                await update.message.reply_text("Введите город установки:" + step_suffix(1))
                return ADDR_CITY
        else:
            context.user_data.clear()
            await update.message.reply_text("Введите город установки:" + step_suffix(1))
            return ADDR_CITY
    except Exception as e:
        logger.error(f"Ошибка в new_report: {e}")
        await update.message.reply_text("Ошибка. Попробуйте позже.")
        return ConversationHandler.END

# Город
async def addr_city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['addr_city'] = update.message.text.strip()
    save_draft(update.effective_user.id, step=0, addr_city=context.user_data['addr_city'])
    await update.message.reply_text(
        f"Город: {context.user_data['addr_city']}\nВсё верно?",
        reply_markup=yes_no_keyboard()
    )
    return ADDR_CITY_CONFIRM

async def addr_city_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_yes":
        await safe_edit_message(query, "Введите улицу:" + step_suffix(2), None)
        return ADDR_STREET
    elif query.data == "confirm_no":
        await safe_edit_message(query, "Введите город установки заново:" + step_suffix(1), None)
        return ADDR_CITY
    else:
        await safe_edit_message(query, "Отмена создания отчёта.", None)
        await context.bot.send_message(chat_id=update.effective_user.id, text="Выберите действие:", reply_markup=get_main_menu(is_admin(update.effective_user.id)))
        delete_draft(update.effective_user.id)
        return ConversationHandler.END

# Улица
async def addr_street_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['addr_street'] = update.message.text.strip()
    save_draft(update.effective_user.id, step=1, addr_street=context.user_data['addr_street'])
    await update.message.reply_text(
        f"Улица: {context.user_data['addr_street']}\nВсё верно?",
        reply_markup=yes_no_keyboard()
    )
    return ADDR_STREET_CONFIRM

async def addr_street_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_yes":
        await safe_edit_message(query, "Введите номер дома:" + step_suffix(3), None)
        return ADDR_HOUSE
    elif query.data == "confirm_no":
        await safe_edit_message(query, "Введите улицу заново:" + step_suffix(2), None)
        return ADDR_STREET
    else:
        await safe_edit_message(query, "Отмена создания отчёта.", None)
        await context.bot.send_message(chat_id=update.effective_user.id, text="Выберите действие:", reply_markup=get_main_menu(is_admin(update.effective_user.id)))
        delete_draft(update.effective_user.id)
        return ConversationHandler.END

# Дом
async def addr_house_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['addr_house'] = update.message.text.strip()
    save_draft(update.effective_user.id, step=2, addr_house=context.user_data['addr_house'])
    await update.message.reply_text(
        f"Номер дома: {context.user_data['addr_house']}\nВсё верно?",
        reply_markup=yes_no_keyboard()
    )
    return ADDR_HOUSE_CONFIRM

async def addr_house_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_yes":
        await safe_edit_message(query, "Введите номер квартиры/офиса (если нет, введите 0 или прочерк):" + step_suffix(4), None)
        return ADDR_APARTMENT
    elif query.data == "confirm_no":
        await safe_edit_message(query, "Введите номер дома заново:" + step_suffix(3), None)
        return ADDR_HOUSE
    else:
        await safe_edit_message(query, "Отмена создания отчёта.", None)
        await context.bot.send_message(chat_id=update.effective_user.id, text="Выберите действие:", reply_markup=get_main_menu(is_admin(update.effective_user.id)))
        delete_draft(update.effective_user.id)
        return ConversationHandler.END

# Квартира
async def addr_apartment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['addr_apartment'] = update.message.text.strip()
    save_draft(update.effective_user.id, step=3, addr_apartment=context.user_data['addr_apartment'])
    await update.message.reply_text(
        f"Квартира/офис: {context.user_data['addr_apartment']}\nВсё верно?",
        reply_markup=yes_no_keyboard()
    )
    return ADDR_APARTMENT_CONFIRM

async def addr_apartment_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_yes":
        context.user_data['photos'] = []
        save_draft(update.effective_user.id, step=4, photos=[])
        await safe_edit_message(
            query,
            "📸 Отправляйте фото установки (не более 5 штук).\n"
            "Вы можете прислать фото снаружи, изнутри, с торца и ответную часть, и обязательно заполненного гарантийного талона.\n"
            "После каждого фото я буду сообщать, сколько ещё можно добавить.\n"
            "Когда загрузите все фото, отправьте команду /done или просто напишите 'готово'.\n"
            "Чтобы убрать последнее загруженное фото, напишите 'удалить'."
            + step_suffix(5),
            None
        )
        return PHOTOS
    elif query.data == "confirm_no":
        await safe_edit_message(query, "Введите номер квартиры заново:" + step_suffix(4), None)
        return ADDR_APARTMENT
    else:
        await safe_edit_message(query, "Отмена создания отчёта.", None)
        await context.bot.send_message(chat_id=update.effective_user.id, text="Выберите действие:", reply_markup=get_main_menu(is_admin(update.effective_user.id)))
        delete_draft(update.effective_user.id)
        return ConversationHandler.END

# ========== ОБРАБОТКА ФОТО (с поддержкой групп) ==========
async def photos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        if not update.message.photo:
            text = update.message.text.lower() if update.message.text else ""
            if text in ["готово", "/done", "хватит", "всё"]:
                if len(context.user_data.get('photos', [])) == 0:
                    await update.message.reply_text("Вы не отправили ни одного фото. Пожалуйста, отправьте хотя бы одно.")
                    return PHOTOS
                # Переход к запросу суммы
                save_draft(user_id, step=5, photos=context.user_data['photos'])
                await update.message.reply_text(
                    "Теперь введите сумму дополнительных расходов (в рублях, неотрицательное число):" + step_suffix(6)
                )
                return EXTRA_EXPENSES
            elif text in ["удалить", "удалить фото", "убрать", "убрать последнее"]:
                photos = context.user_data.get('photos', [])
                if not photos:
                    await update.message.reply_text("Фото ещё не загружены — нечего удалять.")
                    return PHOTOS
                photos.pop()
                save_draft(user_id, step=4, photos=photos)
                await update.message.reply_text(
                    f"Последнее фото удалено. Осталось {len(photos)} из 5.\n"
                    f"Можете отправить ещё фото или напишите 'готово' для завершения."
                )
                return PHOTOS
            else:
                await update.message.reply_text("Пожалуйста, отправьте фото или напишите 'готово'.")
                return PHOTOS

        # Обработка фото
        media_group_id = update.message.media_group_id
        if media_group_id:
            if 'media_groups' not in context.bot_data:
                context.bot_data['media_groups'] = {}
            if media_group_id in context.bot_data['media_groups']:
                context.bot_data['media_groups'][media_group_id]['photos'].append(update.message.photo[-1].file_id)
                return PHOTOS
            else:
                context.bot_data['media_groups'][media_group_id] = {
                    'photos': [update.message.photo[-1].file_id],
                    'user_id': user_id,
                    'chat_id': update.effective_chat.id,
                    'message_id': update.message.message_id
                }
                asyncio.create_task(process_media_group(media_group_id, context, user_id))
                return PHOTOS
        else:
            try:
                photo_file = await update.message.photo[-1].get_file()
            except Exception as e:
                logger.error(f"Ошибка скачивания фото: {e}")
                await update.message.reply_text("❌ Не удалось загрузить фото. Попробуйте ещё раз.")
                return PHOTOS

            if 'photos' not in context.user_data:
                context.user_data['photos'] = []
            context.user_data['photos'].append(photo_file.file_id)
            save_draft(user_id, step=4, photos=context.user_data['photos'])
            current_count = len(context.user_data['photos'])

            if current_count >= 5:
                # Достигнут лимит, сообщаем и просим написать "готово"
                await update.message.reply_text(
                    f"Получено {current_count} фото. Максимум достигнут.\n"
                    f"Пожалуйста, напишите 'готово' для продолжения."
                )
                return PHOTOS
            else:
                remaining = 5 - current_count
                await update.message.reply_text(
                    f"Фото получено! Загружено {current_count} из 5.\n"
                    f"Можете отправить ещё {remaining} фото или напишите 'готово' для завершения."
                )
                return PHOTOS
    except Exception as e:
        logger.error(f"Ошибка в photos_handler: {e}")
        await update.message.reply_text("Ошибка. Попробуйте ещё раз.")
        return PHOTOS

async def process_media_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    await asyncio.sleep(0.7)
    group_data = context.bot_data['media_groups'].pop(media_group_id, None)
    if not group_data:
        return
    photos = group_data['photos']
    if 'photos' not in context.user_data:
        context.user_data['photos'] = []
    context.user_data['photos'].extend(photos)
    save_draft(user_id, step=4, photos=context.user_data['photos'])
    current_count = len(context.user_data['photos'])
    chat_id = group_data['chat_id']
    if current_count >= 5:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Получено {current_count} фото. Максимум достигнут.\n"
                 f"Пожалуйста, напишите 'готово' для продолжения."
        )
    else:
        remaining = 5 - current_count
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Фото получено! Загружено {current_count} из 5.\n"
                 f"Можете отправить ещё {remaining} фото или напишите 'готово' для завершения."
        )

# ========== ДОПОЛНИТЕЛЬНЫЕ РАСХОДЫ ==========
async def extra_expenses_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = float(update.message.text.strip())
        if value < 0:
            raise ValueError
        context.user_data['extra_expenses'] = value
        save_draft(update.effective_user.id, step=5, extra_expenses=value)
        await update.message.reply_text(
            f"Сумма доп. расходов: {value} руб.\nВсё верно?",
            reply_markup=yes_no_keyboard()
        )
        return EXTRA_EXPENSES_CONFIRM
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите неотрицательное число.")
        return EXTRA_EXPENSES

async def extra_expenses_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == "confirm_yes":
        # Завершаем отчёт
        photos = context.user_data.get('photos', [])
        extra = context.user_data['extra_expenses']
        master = get_master_data(user_id)
        if not master:
            await safe_edit_message(query, "Ошибка: мастер не найден.", None)
            delete_draft(user_id)
            return ConversationHandler.END

        report_id = save_report(
            user_id,
            photos,
            extra,
            master['last_name'],
            master['first_name'],
            master['middle_name'],
            context.user_data['addr_city'],
            context.user_data['addr_street'],
            context.user_data['addr_house'],
            context.user_data['addr_apartment']
        )
        delete_draft(user_id)

        admin_text = (
            f"📄 Отчет: {master['last_name']} {master['first_name']} {master['middle_name']}, {master['city']}\n"
            f"📍 Адрес: {context.user_data['addr_city']}, {context.user_data['addr_street']}, д.{context.user_data['addr_house']}, кв.{context.user_data['addr_apartment']}\n\n"
            f"📸 Фото: {len(photos)} шт.\n"
            f"💰 Доп. расходы: {extra} руб.\n"
            f"💳 Банк: {master['bank']}\n"
            f"📱 СБП: {format_phone(master['sbp_phone'])}\n"
            f"👤 Получатель: {master['fio_sbp']}\n"
            f"🆔 Отчет: {report_id}"
        )
        keyboard = [[InlineKeyboardButton("👁 Посмотреть отчет", callback_data=f"view_{report_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        for admin_id in get_admins():
            try:
                await context.bot.send_message(chat_id=int(admin_id), text=admin_text, reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

        # Отправляем финальное сообщение мастеру
        final_text = (
            "Команда KENSUR благодарит Тебя за высокий уровень клиентского сервиса и качественную установку.\n"
            "Отчет отправлен и будет оплачен до конца недели."
        )
        await safe_edit_message(query, final_text, None)
        await context.bot.send_message(chat_id=user_id, text="Выберите действие:", reply_markup=get_main_menu(is_admin(user_id)))
        return ConversationHandler.END

    elif data == "confirm_no":
        await safe_edit_message(query, "Введите сумму дополнительных расходов заново:", None)
        return EXTRA_EXPENSES

    else:  # confirm_cancel
        await safe_edit_message(query, "Отмена создания отчёта.", None)
        await context.bot.send_message(chat_id=user_id, text="Выберите действие:", reply_markup=get_main_menu(is_admin(user_id)))
        delete_draft(user_id)
        return ConversationHandler.END

# ========== ОБРАБОТКА КНОПОК АДМИНИСТРАТОРА ==========
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id

    try:
        if data.startswith("view_"):
            report_id = data.replace("view_", "")
            report = get_report_by_id(report_id)
            if not report:
                await safe_edit_message(query, "Отчет не найден.", None)
                return
            mark_report_viewed(report_id)

            master = get_master_data(report['user_id'])
            if not master:
                await safe_edit_message(query, "Данные мастера не найдены.", None)
                return

            photos_str = report.get('photos', '')
            photo_ids = photos_str.split(',') if photos_str else []

            if photo_ids:
                media_group = []
                for i, pid in enumerate(photo_ids, 1):
                    if i == 1:
                        media_group.append(InputMediaPhoto(media=pid, caption=f"Фото {i} из 5"))
                    else:
                        media_group.append(InputMediaPhoto(media=pid))
                try:
                    await context.bot.send_media_group(chat_id=user_id, media=media_group)
                except Exception as e:
                    logger.error(f"Ошибка отправки медиагруппы: {e}")
                    await context.bot.send_message(chat_id=user_id, text="Не удалось отправить фотографии.")
            else:
                await context.bot.send_message(chat_id=user_id, text="В отчете нет фотографий.")

            detail_text = (
                f"📋 **Полный отчет**\n\n"
                f"👤 Мастер: {master['last_name']} {master['first_name']} {master['middle_name']}\n"
                f"🏙 Город мастера: {master['city']}\n"
                f"📍 Адрес установки: {report['address_city']}, {report['address_street']}, д.{report['address_house']}, кв.{report['address_apartment']}\n"
                f"📞 Телефон: {format_phone(master['phone'])}\n"
                f"💳 Банк: {master['bank']}\n"
                f"📱 СБП: {format_phone(master['sbp_phone'])}\n"
                f"👤 Получатель: {master['fio_sbp']}\n"
                f"💰 Доп. расходы: {report['extra_expenses']} руб.\n"
                f"🕒 Отправлен: {report['submitted_at']}\n"
                f"💳 Статус оплаты: {report['payment_status']}"
            )
            await context.bot.send_message(chat_id=user_id, text=detail_text, parse_mode="Markdown")

            # Проверяем, не занят ли администратор другим процессом
            if 'pay_report_id' in context.user_data or 'awaiting_screenshot_for' in context.user_data:
                keyboard = [
                    [InlineKeyboardButton("✅ Завершить текущий и открыть новый", callback_data=f"force_new_{report_id}")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="force_cancel")]
                ]
                await safe_edit_message(query, "У вас уже есть незавершённый процесс оплаты. Вы можете завершить его и открыть новый отчёт или отменить действие.", InlineKeyboardMarkup(keyboard))
                return
            context.user_data['pay_report_id'] = report_id
            await safe_edit_message(query, "Введите сумму оплаты за установку (в рублях, неотрицательное число):", None)
            return

        elif data.startswith("myreport_"):
            report_id = data.replace("myreport_", "")
            report = get_report_by_id(report_id)
            if not report or str(report.get('user_id')) != str(user_id):
                await safe_edit_message(query, "Отчёт не найден.", None)
                return

            photos_str = report.get('photos', '')
            photo_ids = photos_str.split(',') if photos_str else []
            if photo_ids:
                media_group = [InputMediaPhoto(media=pid) for pid in photo_ids]
                try:
                    await context.bot.send_media_group(chat_id=user_id, media=media_group)
                except Exception as e:
                    logger.error(f"Ошибка отправки медиагруппы (мои отчёты): {e}")

            if report.get('payment_status') == 'оплачено':
                status_line = "✅ Оплачено"
            elif report.get('admin_viewed'):
                status_line = "👁 На рассмотрении у администратора"
            else:
                status_line = "🕓 Ожидает рассмотрения"

            detail_text = (
                f"📋 Отчёт от {report.get('submitted_at')}\n"
                f"📍 Адрес: {report.get('address_city')}, {report.get('address_street')}, "
                f"д.{report.get('address_house')}, кв.{report.get('address_apartment')}\n"
                f"💰 Доп. расходы: {report.get('extra_expenses')} руб.\n"
                f"💳 Статус: {status_line}"
            )

            can_edit = report.get('payment_status') != 'оплачено' and not report.get('admin_viewed')
            keyboard = []
            if can_edit:
                keyboard.append([InlineKeyboardButton("✏️ Изменить", callback_data=f"editreport_{report_id}")])
                keyboard.append([InlineKeyboardButton("❌ Отменить отчёт", callback_data=f"cancelreport_{report_id}")])
            else:
                detail_text += "\n\nРедактирование недоступно: отчёт уже просмотрен администратором или оплачен."
            await context.bot.send_message(
                chat_id=user_id,
                text=detail_text,
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
            )
            return

        elif data.startswith("cancelreportconfirm_"):
            report_id = data.replace("cancelreportconfirm_", "")
            report = get_report_by_id(report_id)
            if not report or str(report.get('user_id')) != str(user_id):
                await safe_edit_message(query, "Отчёт не найден.", None)
                return
            if report.get('payment_status') == 'оплачено' or report.get('admin_viewed'):
                await safe_edit_message(query, "Отчёт уже просмотрен администратором или оплачен — отменить нельзя.", None)
                return
            delete_report_row(report_id)
            await safe_edit_message(query, "❌ Отчёт отменён.", None)
            master = get_master_data(user_id)
            fio = f"{master['last_name']} {master['first_name']} {master['middle_name']}".strip() if master else str(user_id)
            for admin_id in get_admins():
                try:
                    await context.bot.send_message(
                        chat_id=int(admin_id),
                        text=f"⚠️ Мастер {fio} отменил свой отчёт по адресу: "
                             f"{report.get('address_city')}, {report.get('address_street')}, "
                             f"д.{report.get('address_house')}, кв.{report.get('address_apartment')}."
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить админа {admin_id} об отмене отчёта: {e}")
            return

        elif data == "cancelreportback":
            await safe_edit_message(query, "Действие отменено.", None)
            return

        elif data.startswith("cancelreport_"):
            report_id = data.replace("cancelreport_", "")
            report = get_report_by_id(report_id)
            if not report or str(report.get('user_id')) != str(user_id):
                await safe_edit_message(query, "Отчёт не найден.", None)
                return
            if report.get('payment_status') == 'оплачено' or report.get('admin_viewed'):
                await safe_edit_message(query, "Отчёт уже просмотрен администратором или оплачен — отменить нельзя.", None)
                return
            keyboard = [
                [InlineKeyboardButton("✅ Да, отменить", callback_data=f"cancelreportconfirm_{report_id}")],
                [InlineKeyboardButton("↩️ Назад", callback_data="cancelreportback")]
            ]
            await safe_edit_message(query, "Точно отменить этот отчёт? Отменённый отчёт нельзя будет восстановить.", InlineKeyboardMarkup(keyboard))
            return

        elif data.startswith("force_new_"):
            report_id = data.replace("force_new_", "")
            # Очищаем старые данные
            context.user_data.pop('pay_report_id', None)
            context.user_data.pop('payment_amount', None)
            context.user_data.pop('awaiting_amount_confirm', None)
            context.user_data.pop('awaiting_screenshot_for', None)
            context.user_data['pay_report_id'] = report_id
            await safe_edit_message(query, "Введите сумму оплаты за установку (в рублях, неотрицательное число):", None)
            return
        elif data == "force_cancel":
            await safe_edit_message(query, "Действие отменено.", None)
            return

        elif data.startswith("pay_"):
            report_id = data.replace("pay_", "")
            report = get_report_by_id(report_id)
            if not report:
                await safe_edit_message(query, "Отчет не найден.", None)
                return
            if not is_admin(user_id):
                await safe_edit_message(query, "У вас нет прав для этого действия.", None)
                return

            success = mark_report_paid(report_id)
            if success:
                # Уведомляем мастера о подтверждении оплаты
                master_id = int(report['user_id'])
                confirm_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Подтверждаю оплату", callback_data=f"confirm_{report_id}")]
                ])
                try:
                    await context.bot.send_message(
                        chat_id=master_id,
                        text="Ваш отчет отмечен администратором как оплаченный. Пожалуйста, подтвердите получение денег.",
                        reply_markup=confirm_keyboard
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление мастеру: {e}")

                # Сообщаем администратору об успехе и предлагаем отправить скриншот
                await safe_edit_message(query, f"✅ Отчет {report_id} отмечен как оплаченный.", None)
                if 'awaiting_screenshot_for' not in context.user_data:
                    context.user_data['awaiting_screenshot_for'] = report_id
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="Теперь вы можете отправить скриншот перевода мастеру. Отправьте фото или введите /skip, чтобы пропустить."
                    )
                else:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="Внимание: у вас уже есть ожидающий скриншот. Сначала завершите его или пропустите командой /skip, затем попробуйте снова."
                    )
            else:
                await safe_edit_message(query, "❌ Не удалось отметить отчет как оплаченный. Проверьте ID отчета.", None)

        elif data.startswith("confirm_"):
            report_id = data.replace("confirm_", "")
            report = get_report_by_id(report_id)
            if not report:
                await safe_edit_message(query, "Отчет не найден.", None)
                return
            if user_id != int(report['user_id']):
                await safe_edit_message(query, "Это не ваш отчёт.", None)
                return

            success = mark_master_confirmed(report_id)
            if success:
                try:
                    await query.edit_message_text(
                        text="✅ Спасибо! Подтверждение получено.",
                        reply_markup=None
                    )
                except Exception as e:
                    logger.warning(f"Не удалось отредактировать сообщение: {e}")
                    await context.bot.send_message(chat_id=user_id, text="✅ Спасибо! Подтверждение получено.", reply_markup=get_main_menu(is_admin(user_id)))
                else:
                    await context.bot.send_message(chat_id=user_id, text="Выберите действие:", reply_markup=get_main_menu(is_admin(user_id)))

                master = get_master_data(user_id)
                fio = f"{master['last_name']} {master['first_name']} {master['middle_name']}"
                addr = f"{report['address_city']}, {report['address_street']}, д.{report['address_house']}, кв.{report['address_apartment']}"
                for admin_id in get_admins():
                    try:
                        await context.bot.send_message(
                            chat_id=int(admin_id),
                            text=f"🔔 Мастер {fio} подтвердил получение оплаты по адресу: {addr}"
                        )
                    except Exception as e:
                        logger.error(f"Не удалось уведомить админа {admin_id}: {e}")
            else:
                await safe_edit_message(query, "❌ Не удалось подтвердить оплату. Попробуйте позже.", None)

        elif data.startswith("stats_master_month_"):
            await stats_master_month_callback(update, context)
        elif data.startswith("stats_admin_month_"):
            await stats_admin_month_callback(update, context)
        elif data == "stats_close":
            await safe_edit_message(query, "Окно закрыто.", None)

    except Exception as e:
        logger.error(f"Ошибка в button_callback: {e}")
        try:
            await query.edit_message_text("Произошла ошибка. Попробуйте позже.")
        except:
            await context.bot.send_message(chat_id=user_id, text="Произошла ошибка. Попробуйте позже.")

# ========== ОБРАБОТКА СКРИНШОТА ОТ АДМИНИСТРАТОРА ==========
async def screenshot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    report_id = context.user_data.get('awaiting_screenshot_for')
    if not report_id:
        # Если нет ожидающего скриншота, просто игнорируем
        return

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        report = get_report_by_id(report_id)
        if not report:
            await update.message.reply_text("Ошибка: отчёт не найден.")
            del context.user_data['awaiting_screenshot_for']
            return
        master_id = int(report['user_id'])
        try:
            await context.bot.send_photo(
                chat_id=master_id,
                photo=file_id,
                caption=f"Скриншот перевода по вашему отчёту (ID: {report_id})"
            )
            await update.message.reply_text("✅ Скриншот отправлен мастеру.")
        except Exception as e:
            logger.error(f"Ошибка при отправке скриншота: {e}")
            await update.message.reply_text("❌ Не удалось отправить скриншот. Попробуйте позже.")
        finally:
            del context.user_data['awaiting_screenshot_for']
    else:
        # Не фото – обработчик сработает только для фото, но на всякий случай
        pass

# ========== ПРОПУСК ОТПРАВКИ СКРИНШОТА ==========
async def skip_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if 'awaiting_screenshot_for' in context.user_data:
        del context.user_data['awaiting_screenshot_for']
        await update.message.reply_text("❌ Отправка скриншота пропущена.")
    else:
        await update.message.reply_text("Нет ожидающей отправки скриншота.")

# ========== ОБРАБОТКА СУММЫ ОПЛАТЫ ОТ АДМИНА ==========
async def payment_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    report_id = context.user_data.get('pay_report_id')
    if not report_id:
        await update.message.reply_text("❌ Сначала откройте отчет, нажав кнопку «Посмотреть отчет».")
        return

    try:
        amount = float(update.message.text.strip())
        if amount < 0:
            raise ValueError
        context.user_data['payment_amount'] = amount
        keyboard = [
            [InlineKeyboardButton("✅ Да", callback_data="amount_yes"),
             InlineKeyboardButton("🔄 Изменить", callback_data="amount_no")],
            [InlineKeyboardButton("❌ Отмена", callback_data="amount_cancel")]
        ]
        await update.message.reply_text(
            f"Сумма оплаты за установку: {amount} руб.\nВсё верно?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['awaiting_amount_confirm'] = True
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите неотрицательное число.")

# ========== ОБРАБОТКА ПОДТВЕРЖДЕНИЯ СУММЫ ОПЛАТЫ ==========
async def amount_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if not is_admin(user_id):
        await safe_edit_message(query, "У вас нет прав для этого действия.", None)
        return

    report_id = context.user_data.get('pay_report_id')
    if not report_id:
        await safe_edit_message(query, "Ошибка: идентификатор отчета не найден.", None)
        return

    if data == "amount_yes":
        amount = context.user_data.get('payment_amount')
        if amount is None:
            await safe_edit_message(query, "Ошибка: сумма не найдена.", None)
            return
        success = update_report_payment_amount(report_id, str(amount))
        if success:
            await safe_edit_message(query, f"✅ Сумма {amount} руб. сохранена. Теперь вы можете отметить отчёт как оплаченный.", None)
            pay_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Отметить оплаченным", callback_data=f"pay_{report_id}")]
            ])
            await context.bot.send_message(chat_id=user_id, text="Нажмите кнопку ниже, чтобы завершить оплату:", reply_markup=pay_keyboard)
        else:
            await safe_edit_message(query, "❌ Не удалось сохранить сумму.", None)
        del context.user_data['pay_report_id']
        del context.user_data['payment_amount']
        if 'awaiting_amount_confirm' in context.user_data:
            del context.user_data['awaiting_amount_confirm']
    elif data == "amount_no":
        await safe_edit_message(query, "Введите сумму оплаты заново:", None)
        if 'payment_amount' in context.user_data:
            del context.user_data['payment_amount']
    else:  # amount_cancel
        await safe_edit_message(query, "Операция отменена.", None)
        del context.user_data['pay_report_id']
        if 'payment_amount' in context.user_data:
            del context.user_data['payment_amount']
        if 'awaiting_amount_confirm' in context.user_data:
            del context.user_data['awaiting_amount_confirm']

# ========== ВСПОМОГАТЕЛЬНЫЕ КОМАНДЫ ==========
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    delete_draft(user_id)
    await update.message.reply_text("Операция отменена.", reply_markup=get_main_menu(is_admin(user_id)))
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await update.message.reply_text(
        "Команды:\n"
        "/start - регистрация или главное меню\n"
        "/new_report - создать отчет\n"
        "/edit_profile - изменить СБП-реквизиты\n"
        "/skip - пропустить отправку скриншота\n"
        "/cancel - отменить текущее действие\n"
        "/monthly_summary_now - прогнать месячную сводку сейчас (только для админов)\n"
        "/help - это сообщение",
        reply_markup=get_main_menu(is_admin(user_id))
    )

# ========== ЗАПУСК БОТА (АВТООПРЕДЕЛЕНИЕ РЕЖИМА) ==========
def main():
    # Проверяем, запущены ли мы на Render (есть переменная окружения RENDER_EXTERNAL_URL)
    if "RENDER_EXTERNAL_URL" in os.environ:
        # Режим Render – webhook
        port = int(os.environ.get("PORT", 8000))
        render_url = os.environ["RENDER_EXTERNAL_URL"]
        logger.info(f"Запуск на Render, URL: {render_url}, порт: {port}")

        # Создаём приложение без Updater (он не нужен для webhook)
        app = Application.builder().token(TOKEN).updater(None).connect_timeout(30).read_timeout(30).write_timeout(30).build()

        # Добавляем все обработчики (как обычно)
        reg_conv = ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                LAST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, last_name_handler)],
                FIRST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, first_name_handler)],
                MIDDLE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, middle_name_handler)],
                CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, city_handler)],
                PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_handler)],
                BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, bank_handler)],
                SBP_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sbp_phone_handler)],
                FIO_SBP: [MessageHandler(filters.TEXT & ~filters.COMMAND, fio_sbp_handler)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
        app.add_handler(reg_conv)

        edit_conv = ConversationHandler(
            entry_points=[CommandHandler("edit_profile", edit_profile), MessageHandler(filters.Text("✏️ Изменить СБП-реквизиты"), edit_profile)],
            states={
                EDIT_CHOICE: [CallbackQueryHandler(edit_choice_callback)],
                EDIT_SBP_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_sbp_phone_handler)],
                EDIT_FIO_SBP: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_fio_sbp_handler)],
                EDIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_confirm_handler)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
        app.add_handler(edit_conv)

        report_conv = ConversationHandler(
            entry_points=[
                CommandHandler("new_report", new_report),
                MessageHandler(filters.Text("📸 Новая установка"), new_report)
            ],
            states={
                ADDR_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, addr_city_handler)],
                ADDR_CITY_CONFIRM: [CallbackQueryHandler(addr_city_confirm_callback)],
                ADDR_STREET: [MessageHandler(filters.TEXT & ~filters.COMMAND, addr_street_handler)],
                ADDR_STREET_CONFIRM: [CallbackQueryHandler(addr_street_confirm_callback)],
                ADDR_HOUSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addr_house_handler)],
                ADDR_HOUSE_CONFIRM: [CallbackQueryHandler(addr_house_confirm_callback)],
                ADDR_APARTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addr_apartment_handler)],
                ADDR_APARTMENT_CONFIRM: [CallbackQueryHandler(addr_apartment_confirm_callback)],
                PHOTOS: [
                    MessageHandler(filters.PHOTO, photos_handler),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, photos_handler)
                ],
                EXTRA_EXPENSES: [MessageHandler(filters.TEXT & ~filters.COMMAND, extra_expenses_handler)],
                EXTRA_EXPENSES_CONFIRM: [CallbackQueryHandler(extra_expenses_confirm_callback)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
        app.add_handler(report_conv)

        edit_report_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(edit_report_start, pattern="^editreport_")],
            states={
                EDITREPORT_CHOICE: [CallbackQueryHandler(edit_report_choice_callback)],
                EDITREPORT_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_report_address_handler)],
                EDITREPORT_EXPENSES: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_report_expenses_handler)],
                EDITREPORT_PHOTOS: [
                    MessageHandler(filters.PHOTO, edit_report_photos_handler),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, edit_report_photos_handler)
                ],
                EDITREPORT_CONFIRM: [CallbackQueryHandler(edit_report_confirm_callback)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
        app.add_handler(edit_report_conv)

        app.add_handler(CallbackQueryHandler(amount_confirm_callback, pattern="^amount_"))
        app.add_handler(CallbackQueryHandler(button_callback))

        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("skip", skip_screenshot))
        app.add_handler(CommandHandler("monthly_summary_now", monthly_summary_now_command))

        app.add_handler(MessageHandler(filters.PHOTO, screenshot_handler))

        menu_buttons = ["📸 Новая установка", "📋 Мои отчёты", "📊 Статистика", "📊 Результат мастеров", "✏️ Изменить СБП-реквизиты"]
        app.add_handler(MessageHandler(filters.Text(menu_buttons) & ~filters.COMMAND, menu_handler))

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, payment_amount_handler))

        # Автосводка 1-го числа каждого месяца в 9:00 по Москве
        app.job_queue.run_monthly(
            send_monthly_summaries,
            when=dt_time(hour=9, minute=0, tzinfo=ZoneInfo("Europe/Moscow")),
            day=1
        )

        # Настройка вебхука и запуск веб-сервера
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.requests import Request
        from starlette.responses import Response, PlainTextResponse

        async def telegram(request: Request) -> Response:
            await app.update_queue.put(Update.de_json(await request.json(), app.bot))
            return Response()

        async def health(_: Request) -> PlainTextResponse:
            return PlainTextResponse("OK")

        starlette_app = Starlette(routes=[
            Route(f"/telegram", telegram, methods=["POST"]),
            Route("/healthcheck", health, methods=["GET"]),
        ])

        async def run():
            webhook_url = f"{render_url}/telegram"
            await app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
            logger.info(f"Webhook установлен на {webhook_url}")
            async with app:
                await app.start()
                server = uvicorn.Server(
                    uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
                )
                await server.serve()
                await app.stop()

        asyncio.run(run())
    else:
        # Локальный запуск с polling
        logger.info("Локальный запуск с polling")
        app = Application.builder().token(TOKEN).connect_timeout(10).read_timeout(15).write_timeout(15).build()

        # Добавляем все обработчики (те же, что и выше, но можно скопировать)
         # Обработчики callback-запросов
    app.add_handler(CallbackQueryHandler(amount_confirm_callback, pattern="^amount_"))
    app.add_handler(CallbackQueryHandler(button_callback))

    # Команды
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("skip", skip_screenshot))

    # Обработчик фото (должен быть до menu_handler)
    app.add_handler(MessageHandler(filters.PHOTO, screenshot_handler))

    # Обработчик главного меню (теперь только для текстов, совпадающих с кнопками)
    menu_buttons = ["📸 Новая установка", "📋 Мои отчёты", "📊 Статистика", "📊 Результат мастеров", "✏️ Изменить СБП-реквизиты"]
    app.add_handler(MessageHandler(filters.Text(menu_buttons) & ~filters.COMMAND, menu_handler))

    # Обработчик ввода суммы (получает все остальные тексты)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, payment_amount_handler))

    logger.info(f"{BOT_VERSION} запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()