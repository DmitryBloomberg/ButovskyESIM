#!/usr/bin/env python3
"""Single-file Telegram eSIM sales bot. Uses only the Python standard library."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


REGIONS: list[tuple[str, list[str]]] = [
    (
        "Азия",
        [
            "Китай", "Япония", "Южная Корея", "Малайзия", "Гонконг", "Таиланд",
            "Вьетнам", "Сингапур", "Макао", "Тайвань", "Мальдивские Острова",
            "Индонезия", "Дубай", "Саудовская Аравия", "Катар", "Индия",
            "Филипины", "Камбоджа", "Лаос", "Казахстан",
        ],
    ),
    (
        "Европа",
        [
            "Испания", "Италия", "Франция", "Германия", "Турция",
            "Великобритания", "Греция", "Австрия", "Хорватия", "Нидерланды",
            "Норвегия", "Португалия", "Швеция", "Дания", "Швейцария",
            "Россия", "Финляндия", "Польша", "Мальта", "Исландия",
        ],
    ),
    (
        "Южная Америка",
        [
            "Бразилия", "Аргентина", "Чили", "Парагвай", "Уругвай",
            "Колумбия", "Перу", "Венесуэла", "Боливия", "Эквадор",
            "Гайана", "Французская Гвиана", "Суринам",
        ],
    ),
    (
        "Африка",
        [
            "Марокко", "Египет", "ЮАР", "Тунис", "Кения", "Танзания", "Гана",
            "Эфиопия", "Кот-д'Ивуар", "Уганда", "Руанда", "Маврикий",
            "Мадагаскар", "Алжир", "Гвинея-Бисау", "Замбия", "Бенин",
            "Конго, Демократическая Республика", "Ангола",
        ],
    ),
    ("Океания", ["Австралия", "Новая Зеландия"]),
    ("Северная Америка", ["США", "Канада", "Мексика"]),
]

STATUS_LABELS = {
    "new": "Новая заявка",
    "in_work": "В работе",
    "awaiting_payment": "Ожидает оплату",
    "awaiting_confirmation": "Проверка оплаты",
    "awaiting_instruction": "Подготовка инструкции",
    "delivering": "Отправка инструкции",
    "delivered": "Выполнен",
    "cancelled": "Отменён",
}
ACTIVE_STATUSES = set(STATUS_LABELS) - {"delivered", "cancelled"}
MANUAL_STATUSES = [
    ("new", "Новая заявка"),
    ("in_work", "В работе"),
    ("awaiting_payment", "Ожидает оплату"),
    ("awaiting_confirmation", "Проверка оплаты"),
    ("awaiting_instruction", "Подготовка инструкции"),
    ("delivered", "Выполнен"),
    ("cancelled", "Отменён"),
]
PERIODS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 30]
DISCLAIMER = (
    "Важно: после подтверждения оплаты отменить заказ нельзя. eSIM предоставляет "
    "сторонний поставщик; мы не отвечаем за доступность и качество его мобильного "
    "интернета."
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", str(BASE_DIR / "data"))).resolve()
USERS_DIR = DATA_DIR / "users"
ORDERS_FILE = DATA_DIR / "orders.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
USER_STATES_FILE = DATA_DIR / "user_states.json"
BOT_STATE_FILE = DATA_DIR / "bot_state.json"
ADMIN_FLOW_FILE = DATA_DIR / "admin_flow.json"


class TelegramAPIError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(description)
        self.code = code
        self.description = description


class UserAlert(Exception):
    """An expected callback result that should be shown as a Telegram alert."""


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {message}", flush=True)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось прочитать файл данных {path}: {exc}") from exc


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def user_file(telegram_id: int) -> Path:
    return USERS_DIR / f"{telegram_id}.js"


def get_user(telegram_id: int) -> dict[str, Any] | None:
    path = user_file(telegram_id)
    value = read_json(path, None)
    if value is not None and not isinstance(value, dict):
        raise RuntimeError(f"Некорректный профиль пользователя {telegram_id}")
    return value


def save_user(user: dict[str, Any]) -> None:
    user["updated_at"] = timestamp()
    atomic_write_json(user_file(int(user["telegram_id"])), user)


def get_users() -> list[dict[str, Any]]:
    USERS_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    result: list[dict[str, Any]] = []
    for path in USERS_DIR.glob("*.js"):
        if not path.stem.isdigit():
            continue
        user = read_json(path, None)
        if not isinstance(user, dict):
            raise RuntimeError(f"Некорректный профиль пользователя в {path}")
        result.append(user)
    return sorted(result, key=lambda item: int(item["telegram_id"]))


def all_orders() -> list[dict[str, Any]]:
    value = read_json(ORDERS_FILE, [])
    if not isinstance(value, list):
        raise RuntimeError(f"Некорректный список заказов в {ORDERS_FILE}")
    return value


def save_orders(orders: list[dict[str, Any]]) -> None:
    atomic_write_json(ORDERS_FILE, orders)


def find_order(order_id: int) -> dict[str, Any] | None:
    return next((item for item in all_orders() if int(item["id"]) == order_id), None)


def save_order(updated: dict[str, Any]) -> None:
    orders = all_orders()
    for index, order in enumerate(orders):
        if int(order["id"]) == int(updated["id"]):
            orders[index] = updated
            save_orders(orders)
            return
    raise RuntimeError(f"Заказ #{updated['id']} не найден при сохранении")


def get_settings() -> dict[str, Any]:
    value = read_json(
        SETTINGS_FILE,
        {"disabled_regions": [], "disabled_countries": {}, "admin_flow": None},
    )
    if not isinstance(value, dict):
        raise RuntimeError("Файл настроек бота повреждён")
    value.setdefault("disabled_regions", [])
    value.setdefault("disabled_countries", {})
    value.setdefault("admin_flow", None)
    return value


def save_settings(settings: dict[str, Any]) -> None:
    atomic_write_json(SETTINGS_FILE, settings)


def get_user_states() -> dict[str, Any]:
    value = read_json(USER_STATES_FILE, {})
    return value if isinstance(value, dict) else {}


def set_user_state(telegram_id: int, state: str | None, extra: dict[str, Any] | None = None) -> None:
    states = get_user_states()
    key = str(telegram_id)
    if state is None:
        states.pop(key, None)
    else:
        states[key] = {"state": state, **(extra or {})}
    atomic_write_json(USER_STATES_FILE, states)


def get_user_state(telegram_id: int) -> dict[str, Any] | None:
    return get_user_states().get(str(telegram_id))


def country_is_disabled(region_index: int, country_index: int) -> bool:
    settings = get_settings()
    return str(country_index) in settings["disabled_countries"].get(str(region_index), [])


class TelegramBot:
    def __init__(self, token: str, admin_id: int):
        self.token = token
        self.admin_id = admin_id
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def api(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + method,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
                description = str(result.get("description", "Telegram API error"))
            except (ValueError, OSError):
                description = "Telegram API returned an HTTP error"
            raise TelegramAPIError(exc.code, description) from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise TelegramAPIError(0, "network or response error") from None
        if not result.get("ok"):
            raise TelegramAPIError(int(result.get("error_code", 0)), str(result.get("description", "Unknown error")))
        return result.get("result")

    def send(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        elif reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.api("sendMessage", payload)

    def answer_callback(self, query_id: str, text: str | None = None, alert: bool = False) -> None:
        payload: dict[str, Any] = {"callback_query_id": query_id}
        if text:
            payload["text"] = text[:200]
        if alert:
            payload["show_alert"] = True
        try:
            self.api("answerCallbackQuery", payload)
        except TelegramAPIError as exc:
            log(f"callback response failed: {exc.description}")

    def copy_message(
        self,
        to_chat_id: int,
        from_chat_id: int,
        message_id: int,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": to_chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self.api("copyMessage", payload)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
        elif "message" in update:
            self.handle_message(update["message"])

    def handle_message(self, message: dict[str, Any]) -> None:
        sender = message.get("from", {})
        if not sender.get("id"):
            return
        telegram_id = int(sender["id"])
        text = str(message.get("text", "")).strip()
        user = get_user(telegram_id)
        if user and user.get("blocked") and telegram_id != self.admin_id:
            if text.startswith("/start"):
                self.send(telegram_id, "Ваш профиль временно заблокирован. Обратитесь в поддержку.")
            return

        if text.startswith("/start"):
            self.start_user(sender)
            return
        if text == "/cancel":
            if telegram_id == self.admin_id and get_settings().get("admin_flow"):
                settings = get_settings()
                settings["admin_flow"] = None
                save_settings(settings)
                self.send(
                    telegram_id,
                    "Текущий этап заказа сброшен.",
                    [[{"text": "Админ-панель", "callback_data": "admin:home"}]],
                )
                return
            set_user_state(telegram_id, None)
            self.send_home(telegram_id)
            return
        if message.get("contact"):
            self.handle_contact(telegram_id, message["contact"])
            return
        if not user:
            self.start_user(sender)
            return

        state = get_user_state(telegram_id)
        if state and state.get("state") == "awaiting_phone":
            if self.store_phone(telegram_id, text):
                set_user_state(telegram_id, None)
                self.send(
                    telegram_id,
                    "Номер сохранён. Добро пожаловать!",
                    reply_markup={"remove_keyboard": True},
                )
                self.send_home(telegram_id)
            else:
                self.send(telegram_id, "Введите номер в международном формате, например +79991234567.")
            return
        if state and state.get("state") == "compat_model":
            set_user_state(telegram_id, None)
            self.send(telegram_id, compatibility_result(text))
            self.send_home(telegram_id)
            return
        if state and state.get("state") == "awaiting_receipt":
            self.handle_receipt(message, user)
            return

        if telegram_id == self.admin_id:
            self.handle_admin_message(message, text)
            return

        if text:
            self.send_home(telegram_id)

    def start_user(self, sender: dict[str, Any]) -> None:
        telegram_id = int(sender["id"])
        user = get_user(telegram_id)
        if user is None:
            user = {
                "telegram_id": telegram_id,
                "name": str(sender.get("first_name", "")),
                "last_name": str(sender.get("last_name", "")),
                "username": str(sender.get("username", "")),
                "phone": "",
                "balance": 0,
                "blocked": False,
                "created_at": timestamp(),
                "updated_at": timestamp(),
            }
            save_user(user)
        else:
            user["name"] = str(sender.get("first_name", user.get("name", "")))
            user["last_name"] = str(sender.get("last_name", user.get("last_name", "")))
            user["username"] = str(sender.get("username", user.get("username", "")))
            save_user(user)
        if user.get("blocked"):
            self.send(telegram_id, "Ваш профиль временно заблокирован. Обратитесь в поддержку.")
            return
        if not user.get("phone"):
            set_user_state(telegram_id, "awaiting_phone")
            self.send(
                telegram_id,
                "Для создания профиля отправьте номер телефона кнопкой ниже или введите его вручную.",
                reply_markup={
                    "keyboard": [[{"text": "Отправить мой номер", "request_contact": True}]],
                    "resize_keyboard": True,
                    "one_time_keyboard": True,
                },
            )
            return
        self.send_home(telegram_id)

    def handle_contact(self, telegram_id: int, contact: dict[str, Any]) -> None:
        if int(contact.get("user_id", -1)) != telegram_id:
            self.send(telegram_id, "Можно сохранить только ваш собственный номер телефона.")
            return
        user = get_user(telegram_id)
        if not user:
            self.send(telegram_id, "Сначала отправьте команду /start.")
            return
        user["phone"] = str(contact.get("phone_number", ""))
        save_user(user)
        set_user_state(telegram_id, None)
        self.send(
            telegram_id,
            "Номер сохранён. Добро пожаловать!",
            reply_markup={"remove_keyboard": True},
        )
        self.send_home(telegram_id)

    @staticmethod
    def store_phone(telegram_id: int, value: str) -> bool:
        normalized = re.sub(r"[\s().-]", "", value)
        if not re.fullmatch(r"\+?[0-9]{7,15}", normalized):
            return False
        user = get_user(telegram_id)
        if not user:
            return False
        user["phone"] = normalized
        save_user(user)
        return True

    def send_home(self, telegram_id: int) -> None:
        user = get_user(telegram_id)
        if not user:
            self.start_user({"id": telegram_id})
            return
        keyboard = [
            [{"text": "Заказать E-SIM", "callback_data": "user:new"}],
            [{"text": "Проверка совместимости", "callback_data": "user:compat"}],
            [{"text": "Профиль", "callback_data": "user:profile"}],
            [{"text": "Активные заказы", "callback_data": "user:orders"}],
        ]
        if telegram_id == self.admin_id:
            keyboard.append([{"text": "Админ-панель", "callback_data": "admin:home"}])
        active_count = sum(
            1
            for order in all_orders()
            if int(order["user_id"]) == telegram_id and order.get("status") in ACTIVE_STATUSES
        )
        self.send(
            telegram_id,
            f"Главное меню\nАктивных заказов: {active_count}\n\n{DISCLAIMER}",
            keyboard,
            reply_markup={"remove_keyboard": True},
        )

    def handle_callback(self, query: dict[str, Any]) -> None:
        query_id = str(query.get("id", ""))
        data = str(query.get("data", ""))
        sender = query.get("from", {})
        if not sender.get("id"):
            self.answer_callback(query_id, "Не удалось определить профиль.", True)
            return
        telegram_id = int(sender["id"])
        alert_text: str | None = None
        show_alert = False
        try:
            if telegram_id != self.admin_id:
                user = get_user(telegram_id)
                if user and user.get("blocked"):
                    raise UserAlert("Профиль временно заблокирован. Обратитесь в поддержку.")
                if not user and not data.startswith("user:"):
                    raise UserAlert("Сначала отправьте /start.")
            self.route_callback(telegram_id, data)
        except UserAlert as exc:
            alert_text = str(exc)
            show_alert = True
        except TelegramAPIError as exc:
            log(f"Telegram callback action failed: {exc.description}")
            alert_text = "Не удалось выполнить действие. Попробуйте ещё раз."
            show_alert = True
        except Exception as exc:
            log(f"Callback processing error: {type(exc).__name__}: {exc}")
            alert_text = "Произошла внутренняя ошибка. Сообщите администратору."
            show_alert = True
        self.answer_callback(query_id, alert_text, show_alert)

    def route_callback(self, telegram_id: int, data: str) -> None:
        parts = data.split(":")
        if data == "user:new":
            self.show_regions(telegram_id)
        elif data == "user:compat":
            set_user_state(telegram_id, "compat_model")
            self.send(
                telegram_id,
                "Отправьте точную модель устройства и, если знаете, версию ОС. "
                "Например: iPhone 15 Pro, iOS 18.",
            )
        elif data == "user:profile":
            self.show_profile(telegram_id)
        elif data == "user:phone":
            self.request_phone(telegram_id)
        elif data == "user:orders":
            self.show_user_orders(telegram_id)
        elif data == "user:home":
            self.send_home(telegram_id)
        elif data == "user:pay" and len(parts) == 3:
            self.request_receipt(telegram_id, int(parts[2]))
        elif data == "user:cancel":
            self.send_home(telegram_id)
        elif data == "admin:home":
            self.require_admin(telegram_id)
            self.show_admin_home(telegram_id)
        elif len(parts) >= 2 and parts[0] == "region":
            self.select_region(telegram_id, int(parts[1]))
        elif len(parts) == 4 and parts[0] == "countries":
            self.show_countries(telegram_id, int(parts[1]), int(parts[2]), int(parts[3]))
        elif len(parts) == 3 and parts[0] == "country":
            self.select_country(telegram_id, int(parts[1]), int(parts[2]))
        elif len(parts) == 3 and parts[0] == "submit":
            self.submit_order(telegram_id, int(parts[1]), int(parts[2]))
        elif len(parts) == 2 and parts[0] == "order":
            self.show_user_order(telegram_id, int(parts[1]))
        elif parts[0] == "adm" and len(parts) >= 2:
            self.route_admin_callback(telegram_id, parts)
        else:
            raise UserAlert("Эта кнопка устарела. Откройте меню заново.")

    def show_regions(self, telegram_id: int) -> None:
        settings = get_settings()
        disabled = {int(value) for value in settings["disabled_regions"]}
        keyboard = [
            [
                {
                    "text": f"{'⛔ ' if index in disabled else ''}{region}",
                    "callback_data": f"region:{index}",
                }
            ]
            for index, (region, _) in enumerate(REGIONS)
        ]
        keyboard.append([{"text": "Отмена", "callback_data": "user:cancel"}])
        self.send(telegram_id, "Выберите направление:", keyboard)

    def select_region(self, telegram_id: int, region_index: int) -> None:
        if not 0 <= region_index < len(REGIONS):
            raise UserAlert("Направление не найдено.")
        settings = get_settings()
        if region_index in {int(value) for value in settings["disabled_regions"]}:
            raise UserAlert("Это направление временно недоступно для покупки.")
        self.show_countries(telegram_id, region_index, 0, 8)

    def show_countries(
        self, telegram_id: int, region_index: int, page: int, page_size: int = 8
    ) -> None:
        if not 0 <= region_index < len(REGIONS):
            raise UserAlert("Направление не найдено.")
        region_name, countries = REGIONS[region_index]
        if page < 0 or page * page_size >= len(countries):
            raise UserAlert("Список стран не найден.")
        disabled = {
            int(value)
            for value in get_settings()["disabled_countries"].get(str(region_index), [])
        }
        keyboard: list[list[dict[str, str]]] = []
        start = page * page_size
        for country_index in range(start, min(start + page_size, len(countries))):
            country = countries[country_index]
            prefix = "⛔ " if country_index in disabled else ""
            keyboard.append(
                [
                    {
                        "text": prefix + country,
                        "callback_data": f"country:{region_index}:{country_index}",
                    }
                ]
            )
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append(
                {"text": "Назад", "callback_data": f"countries:{region_index}:{page - 1}:{page_size}"}
            )
        if start + page_size < len(countries):
            nav.append(
                {"text": "Далее", "callback_data": f"countries:{region_index}:{page + 1}:{page_size}"}
            )
        if nav:
            keyboard.append(nav)
        keyboard.append([{"text": "К направлениям", "callback_data": "user:new"}])
        keyboard.append([{"text": "Отмена", "callback_data": "user:cancel"}])
        self.send(telegram_id, f"Направление: {region_name}\nВыберите страну:", keyboard)

    def select_country(self, telegram_id: int, region_index: int, country_index: int) -> None:
        if not 0 <= region_index < len(REGIONS):
            raise UserAlert("Направление не найдено.")
        countries = REGIONS[region_index][1]
        if not 0 <= country_index < len(countries):
            raise UserAlert("Страна не найдена.")
        settings = get_settings()
        if region_index in {int(value) for value in settings["disabled_regions"]}:
            raise UserAlert("Это направление временно недоступно для покупки.")
        if str(country_index) in settings["disabled_countries"].get(str(region_index), []):
            raise UserAlert("Эта страна временно недоступна для покупки.")
        keyboard = [
            [{"text": "Оформить заказ", "callback_data": f"submit:{region_index}:{country_index}"}],
            [{"text": "Отмена", "callback_data": "user:cancel"}],
        ]
        self.send(
            telegram_id,
            f"Направление: {REGIONS[region_index][0]}\n"
            f"Страна: {countries[country_index]}\n\n"
            f"{DISCLAIMER}",
            keyboard,
        )

    def submit_order(self, telegram_id: int, region_index: int, country_index: int) -> None:
        # Re-check availability at submission, not just when showing the country.
        if not 0 <= region_index < len(REGIONS):
            raise UserAlert("Направление не найдено.")
        if not 0 <= country_index < len(REGIONS[region_index][1]):
            raise UserAlert("Страна не найдена.")
        settings = get_settings()
        if region_index in {int(value) for value in settings["disabled_regions"]}:
            raise UserAlert("Это направление временно недоступно для покупки.")
        if str(country_index) in settings["disabled_countries"].get(str(region_index), []):
            raise UserAlert("Эта страна временно недоступна для покупки.")
        state = read_json(BOT_STATE_FILE, {"next_order_id": 1})
        order_id = int(state.get("next_order_id", 1))
        state["next_order_id"] = order_id + 1
        atomic_write_json(BOT_STATE_FILE, state)
        user = get_user(telegram_id)
        if not user:
            raise UserAlert("Профиль не найден. Отправьте /start.")
        order = {
            "id": order_id,
            "user_id": telegram_id,
            "region": REGIONS[region_index][0],
            "country": REGIONS[region_index][1][country_index],
            "status": "new",
            "created_at": timestamp(),
            "period_days": None,
            "package": None,
            "price_rub": None,
            "payment_details": None,
            "receipt_message_id": None,
            "instruction_message_ids": [],
            "instruction_sent_count": 0,
        }
        orders = all_orders()
        orders.append(order)
        save_orders(orders)
        name = " ".join(filter(None, [user.get("name"), user.get("last_name")])) or "Без имени"
        admin_text = (
            f"Новый заказ #{order_id}\n"
            f"Пользователь: {name}\n"
            f"Telegram ID: {telegram_id}\n"
            f"Телефон: {user.get('phone') or 'не указан'}\n"
            f"Направление: {order['region']}\n"
            f"Страна: {order['country']}"
        )
        admin_keyboard = [[{"text": "Взять в работу", "callback_data": f"adm:take:{order_id}"}]]
        try:
            self.send(self.admin_id, admin_text, admin_keyboard)
        except TelegramAPIError as exc:
            log(f"Could not notify admin for order #{order_id}: {exc.description}")
            self.send(
                telegram_id,
                f"Заявка #{order_id} сохранена. Администратор получит её, когда откроет бота.",
            )
            return
        self.send(
            telegram_id,
            f"Заявка #{order_id} отправлена администратору. Условия и сумму он сообщит отдельно.",
        )

    def show_profile(self, telegram_id: int) -> None:
        user = get_user(telegram_id)
        if not user:
            raise UserAlert("Профиль не найден. Отправьте /start.")
        username = f"@{user['username']}" if user.get("username") else "не указан"
        orders = [item for item in all_orders() if int(item["user_id"]) == telegram_id]
        active = sum(1 for item in orders if item.get("status") in ACTIVE_STATUSES)
        text = (
            "Профиль\n"
            f"Telegram ID: {telegram_id}\n"
            f"Имя: {user.get('name') or 'не указано'}\n"
            f"Фамилия: {user.get('last_name') or 'не указана'}\n"
            f"Username: {username}\n"
            f"Телефон: {user.get('phone') or 'не указан'}\n"
            f"Баланс: {user.get('balance', 0)}\n"
            f"Всего заказов: {len(orders)}\n"
            f"Активных заказов: {active}\n"
            f"Профиль создан: {user.get('created_at', 'неизвестно')}"
        )
        keyboard = [[{"text": "Главное меню", "callback_data": "user:home"}]]
        if not user.get("phone"):
            keyboard.insert(0, [{"text": "Обновить номер", "callback_data": "user:phone"}])
        self.send(telegram_id, text, keyboard)

    def request_phone(self, telegram_id: int) -> None:
        user = get_user(telegram_id)
        if not user:
            raise UserAlert("Профиль не найден. Отправьте /start.")
        set_user_state(telegram_id, "awaiting_phone")
        self.send(
            telegram_id,
            "Отправьте номер телефона кнопкой ниже или введите его вручную.",
            reply_markup={
                "keyboard": [[{"text": "Отправить мой номер", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            },
        )

    def show_user_orders(self, telegram_id: int) -> None:
        orders = [
            item
            for item in all_orders()
            if int(item["user_id"]) == telegram_id and item.get("status") in ACTIVE_STATUSES
        ]
        if not orders:
            self.send(
                telegram_id,
                "Активных заказов нет.",
                [[{"text": "Заказать E-SIM", "callback_data": "user:new"}],
                 [{"text": "Главное меню", "callback_data": "user:home"}]],
            )
            return
        keyboard: list[list[dict[str, str]]] = []
        lines = ["Активные заказы:"]
        for order in sorted(orders, key=lambda item: int(item["id"]), reverse=True):
            lines.append(
                f"#{order['id']} — {order['region']}, {order['country']} — "
                f"{STATUS_LABELS.get(order['status'], order['status'])}"
            )
            keyboard.append(
                [{"text": f"Заказ #{order['id']}", "callback_data": f"order:{order['id']}"}]
            )
        keyboard.append([{"text": "Главное меню", "callback_data": "user:home"}])
        self.send(telegram_id, "\n".join(lines), keyboard)

    def show_user_order(self, telegram_id: int, order_id: int) -> None:
        order = find_order(order_id)
        if not order or int(order["user_id"]) != telegram_id:
            raise UserAlert("Заказ не найден.")
        lines = [
            f"Заказ #{order_id}",
            f"Направление: {order['region']}",
            f"Страна: {order['country']}",
            f"Статус: {STATUS_LABELS.get(order['status'], order['status'])}",
        ]
        if order.get("period_days"):
            lines.append(f"Период: {order['period_days']} дн.")
        if order.get("package"):
            lines.append(f"Пакет: {order['package']}")
        if order.get("price_rub") is not None:
            lines.append(f"Стоимость: {order['price_rub']} RUB")
        keyboard = [[{"text": "К активным заказам", "callback_data": "user:orders"}]]
        if order.get("status") == "awaiting_payment":
            keyboard.insert(
                0, [{"text": "Я оплатил — отправить чек", "callback_data": f"user:pay:{order_id}"}]
            )
        self.send(telegram_id, "\n".join(lines), keyboard)

    def request_receipt(self, telegram_id: int, order_id: int) -> None:
        order = find_order(order_id)
        if (
            not order
            or int(order["user_id"]) != telegram_id
            or order.get("status") != "awaiting_payment"
        ):
            raise UserAlert("Для этого заказа сейчас нельзя отправить чек.")
        set_user_state(telegram_id, "awaiting_receipt", {"order_id": order_id})
        self.send(
            telegram_id,
            f"Пришлите подтверждение оплаты по заказу #{order_id} файлом, фотографией "
            "или сообщением. Администратор проверит его вручную. Для выхода отправьте /cancel.",
        )

    def handle_receipt(self, message: dict[str, Any], user: dict[str, Any]) -> None:
        telegram_id = int(user["telegram_id"])
        state = get_user_state(telegram_id) or {}
        order_id = int(state.get("order_id", 0))
        order = find_order(order_id)
        if not order or order.get("status") != "awaiting_payment":
            set_user_state(telegram_id, None)
            self.send_home(telegram_id)
            return
        message_id = int(message["message_id"])
        order["receipt_message_id"] = message_id
        order["status"] = "awaiting_confirmation"
        order["receipt_received_at"] = timestamp()
        save_order(order)
        set_user_state(telegram_id, None)
        try:
            self.copy_message(self.admin_id, telegram_id, message_id)
            self.send(
                self.admin_id,
                f"Чек для заказа #{order_id}. Пользователь Telegram ID: {telegram_id}.",
                [[
                    {"text": "Подтвердить оплату", "callback_data": f"adm:payok:{order_id}"},
                    {"text": "Отклонить чек", "callback_data": f"adm:payno:{order_id}"},
                ]],
            )
        except TelegramAPIError as exc:
            log(f"Could not send receipt for order #{order_id} to admin: {exc.description}")
        self.send(
            telegram_id,
            f"Подтверждение оплаты по заказу #{order_id} получено и отправлено администратору.",
        )

    def require_admin(self, telegram_id: int) -> None:
        if telegram_id != self.admin_id:
            raise UserAlert("Доступ запрещён.")

    def show_admin_home(self, telegram_id: int) -> None:
        self.require_admin(telegram_id)
        keyboard = [
            [{"text": "Все пользователи", "callback_data": "adm:users:0"}],
            [{"text": "Все заказы", "callback_data": "adm:orders:0"}],
            [{"text": "Доступность направлений", "callback_data": "adm:regions"}],
            [{"text": "Доступность стран", "callback_data": "adm:countryregions"}],
            [{"text": "Главное меню", "callback_data": "user:home"}],
        ]
        self.send(telegram_id, "Админ-панель", keyboard)

    def route_admin_callback(self, telegram_id: int, parts: list[str]) -> None:
        self.require_admin(telegram_id)
        action = parts[1]
        if action == "home":
            self.show_admin_home(telegram_id)
        elif action == "users" and len(parts) == 3:
            self.show_admin_users(telegram_id, int(parts[2]))
        elif action == "user" and len(parts) == 3:
            self.show_admin_user(telegram_id, int(parts[2]))
        elif action == "block" and len(parts) == 3:
            self.toggle_user_block(telegram_id, int(parts[2]))
        elif action == "userorders" and len(parts) == 3:
            self.show_admin_user_orders(telegram_id, int(parts[2]))
        elif action == "orders" and len(parts) == 3:
            self.show_admin_orders(telegram_id, int(parts[2]))
        elif action == "order" and len(parts) == 3:
            self.show_admin_order(telegram_id, int(parts[2]))
        elif action == "take" and len(parts) == 3:
            self.take_order(telegram_id, int(parts[2]))
        elif action == "period" and len(parts) == 3:
            self.ask_period(telegram_id, int(parts[2]))
        elif action == "p" and len(parts) == 4:
            self.choose_period(telegram_id, int(parts[2]), int(parts[3]))
        elif action == "qpage" and len(parts) == 4:
            self.show_quota_page(telegram_id, int(parts[2]), int(parts[3]))
        elif action == "q" and len(parts) == 4:
            self.choose_quota(telegram_id, int(parts[2]), parts[3])
        elif action == "payok" and len(parts) == 3:
            self.confirm_payment(telegram_id, int(parts[2]))
        elif action == "payno" and len(parts) == 3:
            self.reject_payment(telegram_id, int(parts[2]))
        elif action == "sendinstruction" and len(parts) == 3:
            self.begin_instruction(telegram_id, int(parts[2]))
        elif action == "instructiondone" and len(parts) == 3:
            self.deliver_instruction(telegram_id, int(parts[2]))
        elif action == "statuses" and len(parts) == 3:
            self.show_status_choices(telegram_id, int(parts[2]))
        elif action == "setstatus" and len(parts) == 4:
            self.set_order_status(telegram_id, int(parts[2]), parts[3])
        elif action == "regions":
            self.show_region_settings(telegram_id)
        elif action == "region" and len(parts) == 3:
            self.toggle_region(telegram_id, int(parts[2]))
        elif action == "countryregions":
            self.show_country_regions(telegram_id)
        elif action == "countries" and len(parts) == 4:
            self.show_country_settings(telegram_id, int(parts[2]), int(parts[3]))
        elif action == "country" and len(parts) == 4:
            self.toggle_country(telegram_id, int(parts[2]), int(parts[3]))
        else:
            raise UserAlert("Эта кнопка устарела. Откройте админ-панель заново.")

    def show_admin_users(self, telegram_id: int, page: int) -> None:
        users = get_users()
        page_size = 10
        if page < 0 or (page and page * page_size >= len(users)):
            raise UserAlert("Страница пользователей не найдена.")
        keyboard: list[list[dict[str, str]]] = []
        for user in users[page * page_size : (page + 1) * page_size]:
            name = " ".join(filter(None, [user.get("name"), user.get("last_name")])) or "Без имени"
            label = f"{'ЗАБЛОКИРОВАН · ' if user.get('blocked') else ''}{name} · {user['telegram_id']}"
            keyboard.append(
                [{"text": label[:60], "callback_data": f"adm:user:{user['telegram_id']}"}]
            )
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "Назад", "callback_data": f"adm:users:{page - 1}"})
        if (page + 1) * page_size < len(users):
            nav.append({"text": "Далее", "callback_data": f"adm:users:{page + 1}"})
        if nav:
            keyboard.append(nav)
        keyboard.append([{"text": "Админ-панель", "callback_data": "admin:home"}])
        self.send(telegram_id, f"Пользователи: {len(users)}. Страница {page + 1}", keyboard)

    def show_admin_user(self, telegram_id: int, target_id: int) -> None:
        user = get_user(target_id)
        if not user:
            raise UserAlert("Пользователь не найден.")
        name = " ".join(filter(None, [user.get("name"), user.get("last_name")])) or "не указано"
        orders = [item for item in all_orders() if int(item["user_id"]) == target_id]
        text = (
            f"Профиль пользователя\nTelegram ID: {target_id}\n"
            f"Имя и фамилия: {name}\n"
            f"Username: @{user.get('username') or 'не указан'}\n"
            f"Телефон: {user.get('phone') or 'не указан'}\n"
            f"Баланс: {user.get('balance', 0)}\n"
            f"Заблокирован: {'да' if user.get('blocked') else 'нет'}\n"
            f"Всего заказов: {len(orders)}\n"
            f"Создан: {user.get('created_at', 'неизвестно')}"
        )
        action = "Разблокировать" if user.get("blocked") else "Заблокировать"
        keyboard = [
            [{"text": action, "callback_data": f"adm:block:{target_id}"}],
            [{"text": "Заказы пользователя", "callback_data": f"adm:userorders:{target_id}"}],
            [{"text": "К пользователям", "callback_data": "adm:users:0"}],
        ]
        self.send(telegram_id, text, keyboard)

    def toggle_user_block(self, telegram_id: int, target_id: int) -> None:
        user = get_user(target_id)
        if not user:
            raise UserAlert("Пользователь не найден.")
        if target_id == self.admin_id:
            raise UserAlert("Нельзя заблокировать администратора.")
        user["blocked"] = not bool(user.get("blocked"))
        save_user(user)
        status = "заблокирован" if user["blocked"] else "разблокирован"
        try:
            self.send(
                target_id,
                f"Ваш профиль {status} администратором.",
            )
        except TelegramAPIError:
            pass
        self.show_admin_user(telegram_id, target_id)

    def show_admin_user_orders(self, telegram_id: int, target_id: int) -> None:
        orders = [item for item in all_orders() if int(item["user_id"]) == target_id]
        keyboard = [
            [
                {
                    "text": f"#{item['id']} · {STATUS_LABELS.get(item['status'], item['status'])}",
                    "callback_data": f"adm:order:{item['id']}",
                }
            ]
            for item in sorted(orders, key=lambda value: int(value["id"]), reverse=True)
        ]
        keyboard.append([{"text": "К профилю", "callback_data": f"adm:user:{target_id}"}])
        self.send(telegram_id, f"Заказы пользователя {target_id}: {len(orders)}", keyboard)

    def show_admin_orders(self, telegram_id: int, page: int) -> None:
        orders = sorted(all_orders(), key=lambda item: int(item["id"]), reverse=True)
        page_size = 10
        if page < 0 or (page and page * page_size >= len(orders)):
            raise UserAlert("Страница заказов не найдена.")
        keyboard: list[list[dict[str, str]]] = []
        for order in orders[page * page_size : (page + 1) * page_size]:
            label = (
                f"#{order['id']} · {order['country']} · "
                f"{STATUS_LABELS.get(order['status'], order['status'])}"
            )
            keyboard.append(
                [{"text": label[:60], "callback_data": f"adm:order:{order['id']}"}]
            )
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "Назад", "callback_data": f"adm:orders:{page - 1}"})
        if (page + 1) * page_size < len(orders):
            nav.append({"text": "Далее", "callback_data": f"adm:orders:{page + 1}"})
        if nav:
            keyboard.append(nav)
        keyboard.append([{"text": "Админ-панель", "callback_data": "admin:home"}])
        self.send(telegram_id, f"Все заказы: {len(orders)}. Страница {page + 1}", keyboard)

    def show_admin_order(self, telegram_id: int, order_id: int) -> None:
        order = find_order(order_id)
        if not order:
            raise UserAlert("Заказ не найден.")
        user = get_user(int(order["user_id"])) or {}
        name = " ".join(filter(None, [user.get("name"), user.get("last_name")])) or "Без имени"
        lines = [
            f"Заказ #{order_id}",
            f"Статус: {STATUS_LABELS.get(order['status'], order['status'])}",
            f"Пользователь: {name} · ID {order['user_id']}",
            f"Телефон: {user.get('phone') or 'не указан'}",
            f"Направление: {order['region']}",
            f"Страна: {order['country']}",
            f"Создан: {order.get('created_at', 'неизвестно')}",
        ]
        if order.get("period_days"):
            lines.append(f"Период: {order['period_days']} дн.")
        if order.get("package"):
            lines.append(f"Пакет: {order['package']}")
        if order.get("price_rub") is not None:
            lines.append(f"Стоимость: {order['price_rub']} RUB")
        keyboard: list[list[dict[str, str]]] = []
        if order.get("status") == "new":
            keyboard.append([{"text": "Взять в работу", "callback_data": f"adm:take:{order_id}"}])
        if order.get("status") == "awaiting_confirmation":
            keyboard.append(
                [
                    {"text": "Подтвердить оплату", "callback_data": f"adm:payok:{order_id}"},
                    {"text": "Отклонить чек", "callback_data": f"adm:payno:{order_id}"},
                ]
            )
        if order.get("status") == "awaiting_instruction":
            keyboard.append(
                [{"text": "Добавить инструкцию", "callback_data": f"adm:sendinstruction:{order_id}"}]
            )
        keyboard.append([{"text": "Изменить статус", "callback_data": f"adm:statuses:{order_id}"}])
        keyboard.append([{"text": "К заказам", "callback_data": "adm:orders:0"}])
        self.send(telegram_id, "\n".join(lines), keyboard)

    def take_order(self, telegram_id: int, order_id: int) -> None:
        order = find_order(order_id)
        if not order or order.get("status") != "new":
            raise UserAlert("Заказ уже взят или его статус изменён.")
        order["status"] = "in_work"
        order["taken_by"] = telegram_id
        order["taken_at"] = timestamp()
        save_order(order)
        self.ask_period(telegram_id, order_id)

    def ask_period(self, telegram_id: int, order_id: int) -> None:
        self.require_admin(telegram_id)
        order = find_order(order_id)
        if not order or order.get("status") not in {"in_work", "awaiting_instruction"}:
            raise UserAlert("Сначала возьмите заказ в работу.")
        settings = get_settings()
        settings["admin_flow"] = {"order_id": order_id, "stage": "period"}
        save_settings(settings)
        keyboard: list[list[dict[str, str]]] = []
        for offset in range(0, len(PERIODS), 5):
            keyboard.append(
                [
                    {"text": f"{days} дн.", "callback_data": f"adm:p:{order_id}:{days}"}
                    for days in PERIODS[offset : offset + 5]
                ]
            )
        self.send(telegram_id, f"Заказ #{order_id}: выберите период действия eSIM.", keyboard)

    def require_admin_flow(self, order_id: int, stage: str) -> dict[str, Any]:
        flow = get_settings().get("admin_flow")
        if not flow or int(flow.get("order_id", -1)) != order_id or flow.get("stage") != stage:
            raise UserAlert("Этап обработки заказа изменился. Откройте заказ заново.")
        order = find_order(order_id)
        if not order:
            raise UserAlert("Заказ не найден.")
        return order

    def choose_period(self, telegram_id: int, order_id: int, days: int) -> None:
        self.require_admin(telegram_id)
        if days not in PERIODS:
            raise UserAlert("Такой срок недоступен.")
        order = self.require_admin_flow(order_id, "period")
        order["period_days"] = days
        save_order(order)
        settings = get_settings()
        settings["admin_flow"] = {"order_id": order_id, "stage": "quota"}
        save_settings(settings)
        self.show_quota_page(telegram_id, order_id, 0)

    def show_quota_page(self, telegram_id: int, order_id: int, page: int) -> None:
        self.require_admin(telegram_id)
        self.require_admin_flow(order_id, "quota")
        options = ["0.5day"] + [str(value) for value in range(1, 51)]
        page_size = 15
        if page < 0 or page * page_size >= len(options):
            raise UserAlert("Страница пакетов не найдена.")
        keyboard: list[list[dict[str, str]]] = []
        for offset in range(page * page_size, min((page + 1) * page_size, len(options)), 3):
            row = []
            for item in options[offset : min(offset + 3, (page + 1) * page_size)]:
                label = "0,5 ГБ/день" if item == "0.5day" else f"{item} ГБ"
                row.append({"text": label, "callback_data": f"adm:q:{order_id}:{item}"})
            keyboard.append(row)
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "Назад", "callback_data": f"adm:qpage:{order_id}:{page - 1}"})
        if (page + 1) * page_size < len(options):
            nav.append({"text": "Далее", "callback_data": f"adm:qpage:{order_id}:{page + 1}"})
        if nav:
            keyboard.append(nav)
        self.send(telegram_id, f"Заказ #{order_id}: выберите пакет трафика.", keyboard)

    def choose_quota(self, telegram_id: int, order_id: int, quota: str) -> None:
        self.require_admin(telegram_id)
        order = self.require_admin_flow(order_id, "quota")
        if quota == "0.5day":
            package = "0,5 ГБ в день"
        elif quota.isdigit() and 1 <= int(quota) <= 50:
            package = f"{int(quota)} ГБ"
        else:
            raise UserAlert("Такой пакет недоступен.")
        order["package"] = package
        save_order(order)
        settings = get_settings()
        settings["admin_flow"] = {"order_id": order_id, "stage": "payment_info"}
        save_settings(settings)
        self.send(
            telegram_id,
            f"Заказ #{order_id}: отправьте сумму в рублях первой строкой, "
            "а ниже — инструкцию или реквизиты для оплаты.\n"
            "Пример:\n1500\nПеревод по номеру ...",
        )

    def handle_admin_message(self, message: dict[str, Any], text: str) -> None:
        if text.startswith("/start") or text == "/admin":
            self.show_admin_home(self.admin_id)
            return
        if text == "/cancel":
            settings = get_settings()
            settings["admin_flow"] = None
            save_settings(settings)
            self.send(self.admin_id, "Текущий этап сброшен.", [[{"text": "Админ-панель", "callback_data": "admin:home"}]])
            return
        flow = get_settings().get("admin_flow")
        if not flow:
            if text:
                self.show_admin_home(self.admin_id)
            return
        order_id = int(flow["order_id"])
        stage = flow.get("stage")
        if stage == "payment_info":
            self.store_payment_info(order_id, text)
        elif stage == "instruction":
            self.collect_instruction(order_id, message)

    def store_payment_info(self, order_id: int, text: str) -> None:
        lines = text.splitlines()
        if not lines:
            self.send(self.admin_id, "Нужно указать сумму в рублях первой строкой.")
            return
        try:
            amount = Decimal(lines[0].strip().replace(",", "."))
        except InvalidOperation:
            self.send(self.admin_id, "Сумма должна быть числом. Пример: 1500")
            return
        if not amount.is_finite() or amount <= 0 or amount > Decimal("10000000"):
            self.send(self.admin_id, "Укажите сумму больше 0 и не выше 10 000 000 RUB.")
            return
        order = self.require_admin_flow(order_id, "payment_info")
        order["price_rub"] = str(amount.normalize())
        order["payment_details"] = "\n".join(lines[1:]).strip()
        order["status"] = "awaiting_payment"
        order["payment_requested_at"] = timestamp()
        save_order(order)
        settings = get_settings()
        settings["admin_flow"] = None
        save_settings(settings)
        message = (
            f"Оплата по заказу #{order_id}\n"
            f"Направление: {order['region']}\nСтрана: {order['country']}\n"
            f"Период: {order['period_days']} дн.\nПакет: {order['package']}\n"
            f"Стоимость: {order['price_rub']} RUB"
        )
        if order["payment_details"]:
            message += f"\n\nКак оплатить:\n{order['payment_details']}"
        message += f"\n\n{DISCLAIMER}\n\nПосле оплаты нажмите кнопку и отправьте подтверждение."
        self.send(
            int(order["user_id"]),
            message,
            [[{"text": "Я оплатил — отправить чек", "callback_data": f"user:pay:{order_id}"}]],
        )
        self.send(self.admin_id, f"Счёт по заказу #{order_id} отправлен пользователю.")

    @staticmethod
    def is_copyable_message(message: dict[str, Any]) -> bool:
        copyable = {
            "text", "photo", "document", "video", "audio", "voice", "video_note",
            "animation", "sticker", "location", "venue", "contact", "poll", "dice",
        }
        return any(key in message for key in copyable)

    def collect_instruction(self, order_id: int, message: dict[str, Any]) -> None:
        order = self.require_admin_flow(order_id, "instruction")
        if not self.is_copyable_message(message):
            self.send(
                self.admin_id,
                "Отправьте инструкцию сообщением, файлом, фото, видео, аудио или другим материалом.",
            )
            return
        message_ids = order.setdefault("instruction_message_ids", [])
        message_ids.append(int(message["message_id"]))
        save_order(order)
        self.send(
            self.admin_id,
            f"Материал добавлен к заказу #{order_id}. Можно отправить ещё или завершить.",
            [[{"text": "Отправить пользователю", "callback_data": f"adm:instructiondone:{order_id}"}]],
        )

    def confirm_payment(self, telegram_id: int, order_id: int) -> None:
        self.require_admin(telegram_id)
        order = find_order(order_id)
        if not order or order.get("status") != "awaiting_confirmation":
            raise UserAlert("Для заказа сейчас нет чека на проверке.")
        order["status"] = "awaiting_instruction"
        order["paid_at"] = timestamp()
        save_order(order)
        self.send(
            int(order["user_id"]),
            f"Оплата по заказу #{order_id} подтверждена. Инструкция будет отправлена после подготовки.",
        )
        self.begin_instruction(telegram_id, order_id)

    def reject_payment(self, telegram_id: int, order_id: int) -> None:
        self.require_admin(telegram_id)
        order = find_order(order_id)
        if not order or order.get("status") != "awaiting_confirmation":
            raise UserAlert("Для заказа сейчас нет чека на проверке.")
        order["status"] = "awaiting_payment"
        order["receipt_message_id"] = None
        save_order(order)
        self.send(
            int(order["user_id"]),
            f"Чек по заказу #{order_id} не подтверждён. Проверьте оплату и отправьте чек повторно.",
            [[{"text": "Отправить чек", "callback_data": f"user:pay:{order_id}"}]],
        )
        self.send(self.admin_id, f"Чек заказа #{order_id} отклонён; статус изменён на «ожидает оплату».")

    def begin_instruction(self, telegram_id: int, order_id: int) -> None:
        self.require_admin(telegram_id)
        order = find_order(order_id)
        if not order or order.get("status") not in {"awaiting_instruction", "delivering"}:
            raise UserAlert("Заказ ещё не ожидает инструкцию.")
        order["status"] = "awaiting_instruction"
        order.setdefault("instruction_message_ids", [])
        order.setdefault("instruction_sent_count", 0)
        save_order(order)
        settings = get_settings()
        settings["admin_flow"] = {"order_id": order_id, "stage": "instruction"}
        save_settings(settings)
        self.send(
            telegram_id,
            f"Заказ #{order_id}: пришлите инструкцию. Принимаются текст, фотографии, документы, "
            "видео, аудио и другие сообщения. После всех материалов нажмите «Отправить пользователю»."
            "\nДля прерывания этапа используйте /cancel.",
        )

    def deliver_instruction(self, telegram_id: int, order_id: int) -> None:
        self.require_admin(telegram_id)
        order = self.require_admin_flow(order_id, "instruction")
        message_ids = order.get("instruction_message_ids", [])
        sent = int(order.get("instruction_sent_count", 0))
        if not message_ids:
            raise UserAlert("Сначала отправьте хотя бы один материал инструкции.")
        order["status"] = "delivering"
        save_order(order)
        while sent < len(message_ids):
            self.copy_message(
                int(order["user_id"]),
                self.admin_id,
                int(message_ids[sent]),
            )
            sent += 1
            order["instruction_sent_count"] = sent
            save_order(order)
        order["status"] = "delivered"
        order["delivered_at"] = timestamp()
        save_order(order)
        settings = get_settings()
        settings["admin_flow"] = None
        save_settings(settings)
        self.send(
            int(order["user_id"]),
            f"Заказ #{order_id} выполнен. Инструкция eSIM отправлена отдельными сообщениями.",
        )
        self.send(self.admin_id, f"Инструкция по заказу #{order_id} отправлена пользователю.")

    def show_status_choices(self, telegram_id: int, order_id: int) -> None:
        self.require_admin(telegram_id)
        if not find_order(order_id):
            raise UserAlert("Заказ не найден.")
        keyboard = [
            [{"text": label, "callback_data": f"adm:setstatus:{order_id}:{status}"}]
            for status, label in MANUAL_STATUSES
        ]
        keyboard.append([{"text": "К заказу", "callback_data": f"adm:order:{order_id}"}])
        self.send(telegram_id, f"Выберите новый статус заказа #{order_id}:", keyboard)

    def set_order_status(self, telegram_id: int, order_id: int, status: str) -> None:
        self.require_admin(telegram_id)
        order = find_order(order_id)
        if not order or status not in STATUS_LABELS:
            raise UserAlert("Заказ или статус не найден.")
        old_status = order["status"]
        order["status"] = status
        order["status_updated_at"] = timestamp()
        order["status_updated_by"] = telegram_id
        save_order(order)
        if status in {"delivered", "cancelled"}:
            settings = get_settings()
            flow = settings.get("admin_flow")
            if flow and int(flow.get("order_id", -1)) == order_id:
                settings["admin_flow"] = None
                save_settings(settings)
        try:
            self.send(
                int(order["user_id"]),
                f"Статус заказа #{order_id} изменён: "
                f"{STATUS_LABELS.get(old_status, old_status)} → {STATUS_LABELS[status]}.",
            )
        except TelegramAPIError:
            pass
        self.show_admin_order(telegram_id, order_id)

    def show_region_settings(self, telegram_id: int) -> None:
        self.require_admin(telegram_id)
        settings = get_settings()
        disabled = {int(value) for value in settings["disabled_regions"]}
        keyboard = [
            [
                {
                    "text": f"{'ВКЛЮЧИТЬ' if index in disabled else 'Закрыть'} · {name}",
                    "callback_data": f"adm:region:{index}",
                }
            ]
            for index, (name, _) in enumerate(REGIONS)
        ]
        keyboard.append([{"text": "Админ-панель", "callback_data": "admin:home"}])
        self.send(telegram_id, "Нажмите, чтобы изменить доступность направления:", keyboard)

    def toggle_region(self, telegram_id: int, region_index: int) -> None:
        self.require_admin(telegram_id)
        if not 0 <= region_index < len(REGIONS):
            raise UserAlert("Направление не найдено.")
        settings = get_settings()
        disabled = {int(value) for value in settings["disabled_regions"]}
        if region_index in disabled:
            disabled.remove(region_index)
        else:
            disabled.add(region_index)
        settings["disabled_regions"] = sorted(disabled)
        save_settings(settings)
        self.show_region_settings(telegram_id)

    def show_country_regions(self, telegram_id: int) -> None:
        self.require_admin(telegram_id)
        keyboard = [
            [{"text": name, "callback_data": f"adm:countries:{index}:0"}]
            for index, (name, _) in enumerate(REGIONS)
        ]
        keyboard.append([{"text": "Админ-панель", "callback_data": "admin:home"}])
        self.send(telegram_id, "Выберите направление для настройки отдельных стран:", keyboard)

    def show_country_settings(self, telegram_id: int, region_index: int, page: int) -> None:
        self.require_admin(telegram_id)
        if not 0 <= region_index < len(REGIONS):
            raise UserAlert("Направление не найдено.")
        region_name, countries = REGIONS[region_index]
        page_size = 12
        if page < 0 or page * page_size >= len(countries):
            raise UserAlert("Страница стран не найдена.")
        disabled = {
            int(value)
            for value in get_settings()["disabled_countries"].get(str(region_index), [])
        }
        keyboard: list[list[dict[str, str]]] = []
        for country_index in range(page * page_size, min((page + 1) * page_size, len(countries))):
            state = "ВКЛЮЧИТЬ" if country_index in disabled else "Закрыть"
            keyboard.append(
                [
                    {
                        "text": f"{state} · {countries[country_index]}",
                        "callback_data": f"adm:country:{region_index}:{country_index}",
                    }
                ]
            )
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "Назад", "callback_data": f"adm:countries:{region_index}:{page - 1}"})
        if (page + 1) * page_size < len(countries):
            nav.append({"text": "Далее", "callback_data": f"adm:countries:{region_index}:{page + 1}"})
        if nav:
            keyboard.append(nav)
        keyboard.append([{"text": "К направлениям", "callback_data": "adm:countryregions"}])
        self.send(telegram_id, f"Доступность стран · {region_name}", keyboard)

    def toggle_country(self, telegram_id: int, region_index: int, country_index: int) -> None:
        self.require_admin(telegram_id)
        if (
            not 0 <= region_index < len(REGIONS)
            or not 0 <= country_index < len(REGIONS[region_index][1])
        ):
            raise UserAlert("Страна не найдена.")
        settings = get_settings()
        country_map = settings["disabled_countries"]
        disabled = {
            int(value) for value in country_map.get(str(region_index), [])
        }
        if country_index in disabled:
            disabled.remove(country_index)
        else:
            disabled.add(country_index)
        country_map[str(region_index)] = sorted(disabled)
        settings["disabled_countries"] = country_map
        save_settings(settings)
        self.show_country_settings(telegram_id, region_index, country_index // 12)


def compatibility_result(model: str) -> str:
    """Conservative baseline guidance; exact regional model variants must be verified."""
    normalized = re.sub(r"\s+", " ", model.strip().lower())
    if not normalized:
        return "Не удалось распознать модель. Пришлите точное название устройства."

    iphone = re.search(r"\biphone\s*(xs\s*max|xs|xr|se\s*\(?\s*2|se\s*\(?\s*3|1[1-7])\b", normalized)
    if iphone:
        version = "iOS 12.1 или новее"
        return (
            f"Вероятно подходит: {model.strip()} поддерживает eSIM начиная с {version}. "
            "Перед покупкой проверьте точную региональную модель устройства, отсутствие "
            "блокировки оператора и доступность пункта «Добавить eSIM» в настройках."
        )
    if re.search(r"\biphone\s*(x|8|7|6|5)\b", normalized):
        return (
            f"Не подходит: {model.strip()} не поддерживает eSIM. "
            "Для iPhone нужна модель XS, XS Max, XR или новее; версия iOS сама по себе "
            "не добавляет поддержку eSIM."
        )

    pixel = re.search(r"\bpixel\s*([3-9]|[1-9][0-9])\b", normalized)
    if pixel:
        generation = int(pixel.group(1))
        if generation >= 3:
            caveat = (
                " У отдельных версий Pixel 3/3a и региональных вариантов есть ограничения."
                if generation in (3, 4)
                else ""
            )
            return (
                f"Вероятно подходит: Google Pixel {generation} и новее обычно поддерживают eSIM."
                f"{caveat} Проверьте полный код модели, регион и отсутствие блокировки оператора."
            )
        return f"Не подходит: Google Pixel {generation} не поддерживает eSIM."

    if re.search(r"\b(samsung|galaxy)\b", normalized):
        if re.search(r"\b(s20|s21|s22|s23|s24|s25|s26)\b", normalized) or re.search(
            r"\b(z\s*(flip|fold)\s*[3-9]|z\s*(flip|fold)\s*(10|11))\b", normalized
        ):
            return (
                f"Вероятно подходит: {model.strip()} относится к линейкам Samsung, "
                "в которых встречаются модели с eSIM. У Samsung поддержка зависит от точного "
                "кода устройства и страны продажи; проверьте код модели и наличие "
                "«Добавить eSIM» в настройках."
            )
        if re.search(r"\b(s10|s9|s8|note\s*(8|9|10)|a[0-9]{2})\b", normalized):
            return (
                f"Вероятно не подходит: для {model.strip()} поддержка eSIM отсутствует "
                "у большинства распространённых региональных версий. Проверьте полный код "
                "модели у производителя перед покупкой."
            )

    return (
        f"Не могу надёжно подтвердить поддержку для «{model.strip()}» без полного кода модели. "
        "Проверьте в настройках наличие «Добавить eSIM» или отправьте модель вида "
        "SM-S921B / Axxxx и страну покупки. Устройства должны быть разблокированы оператором."
    )


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    admin_raw = os.environ.get("ADMIN_ID", "").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Не задан корректный BOT_TOKEN. Проверьте защищённый файл конфигурации.")
    if not re.fullmatch(r"[0-9]+", admin_raw):
        raise SystemExit("Не задан корректный ADMIN_ID. Проверьте защищённый файл конфигурации.")
    admin_id = int(admin_raw)
    if admin_id <= 0:
        raise SystemExit("ADMIN_ID должен быть положительным Telegram ID.")

    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    USERS_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    bot = TelegramBot(token, admin_id)
    offset = 0
    log("Telegram eSIM bot started; waiting for updates.")
    while True:
        try:
            updates = bot.api(
                "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
            )
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                try:
                    bot.handle_update(update)
                except TelegramAPIError as exc:
                    log(f"Update handling failed: {exc.description}")
                except Exception as exc:
                    log(f"Update handling error: {type(exc).__name__}: {exc}")
        except TelegramAPIError as exc:
            if exc.code == 401:
                raise SystemExit("Telegram отклонил BOT_TOKEN. Исправьте токен в конфигурации systemd.")
            log(f"Telegram polling failed: {exc.description}; retrying in 5 seconds.")
            time.sleep(5)


if __name__ == "__main__":
    main()