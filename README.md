# tg_autopilot_full_with_fishing_pause_v10 (REAL)

## Что исправлено
- Архив НЕ пустой :)
- `ImportError is_bite_text` исправлен: функция есть в `game_parser.py`
- Рыбалка:
  - "Подсечь/Тащи" нажимается **только** на текстах поклёвки (BITE_TRIGGERS)
  - Перед нажатием задержка **1–3 секунды** (настройка в .env)
  - Результат ("нет рыбов", "улов", "инвентарь полон") → "Закинуть"
  - Антиспам: если игра пишет "Подожди N секунд" → бот ждёт
- Рыбалка работает даже при `/pause` и при health-pause
- База новая: `data_v7.db` (чтобы не конфликтовать со старым data.db)

## Установка (Windows)
```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py
```

Если при первом входе не приходит код авторизации:
- номер телефона теперь можно просто ввести в консоли при запуске (в `.env` хранить не обязательно);
- при желании можно оставить `PHONE=+79991234567` (или `TELEGRAM_PHONE=...`) для автоподстановки;
- включи SMS-фолбэк: `FORCE_SMS=1` (или `TELEGRAM_FORCE_SMS=1`);
- перезапусти `python main.py` и введи код в консоль.

Альтернативный вход через QR:
- в `.env` укажи `AUTH_MODE=qr` (или `TELEGRAM_AUTH_MODE=qr`);
- запусти `python main.py`;
- скрипт покажет QR-код прямо в терминале (и дублирует ссылку в логи);
- отсканируй QR телефоном в Telegram и подтверди вход.

Вход через готовую сессию:
- сохрани строку в `.env`: `STRING_SESSION=...` (или `TELEGRAM_STRING_SESSION=...`);
- при наличии `STRING_SESSION` вход по коду/QR не потребуется.


v9: добавлены взвешенные человеческие задержки для подсечки и закидывания.


v10: вариант C задержек для леса/боя + 'Вылазка' медленнее; после проигрыша — пауза (по CFG.health_pause_min/max), рыбалка не блокируется.


## LM Studio для решений в данжах

Добавлена интеграция локальной LLM через LM Studio (OpenAI-compatible API).

1. Запусти модель в LM Studio с API-сервером (обычно `http://127.0.0.1:1234/v1`).
2. В `.env` укажи:

```env
DUNGEON_ENABLED=1
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
LMSTUDIO_MODEL=qwen/qwen3-1.7b
LMSTUDIO_TIMEOUT_SEC=20
LMSTUDIO_TEMPERATURE=0.1
LMSTUDIO_MAX_TOKENS=80
```

Когда бот увидит экран данжа с вариантами выбора, он отправит текст и кнопки в LM Studio и нажмёт выбранную кнопку.

Если `LMSTUDIO_MODEL` не найден в `/v1/models`, бот автоматически выберет подходящую non-embedding модель (с приоритетом Qwen, если доступна).
