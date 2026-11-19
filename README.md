# Escrow-бот «Гарант» для P2P-сделок в USDT

Telegram-бот удерживает оплату покупателя и выплачивает её продавцу только после подтверждения
обеих сторон. Архитектура и решения — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

> Проект в разработке. Готово: схема БД с защитными триггерами, ledger, комиссии, автомат сделок,
> создание сделки в боте (мастер, `/sell` `/buy` `/deal`, inline-режим, приглашения, шаблоны).
> Ещё нет: приём оплаты (CryptoBot, сети), отправка уведомлений из outbox, выплаты, админка.
> Полный README с инструкцией по testnet и списком рисков будет в конце разработки.

## Запуск для разработки

Нужны Python 3.12, [uv](https://docs.astral.sh/uv/), PostgreSQL 16 и Redis 7.

```bash
uv sync
cp .env.example .env               # BOT_TOKEN, BOT_USERNAME, DATABASE_URL, REDIS_URL, ADMIN_IDS
cp chains.example.yaml chains.yaml # включите нужные сети, укажите депозитные адреса
uv run alembic upgrade head
uv run python -m app.bot           # long polling, без вебхука
```

Продакшен-режим — вебхук: `docker compose up -d` поднимает `postgres`, `redis`, `migrate` и `web`
(порт 8080, путь `/tg/webhook`). Нужны `WEBHOOK_BASE_URL` (публичный HTTPS через reverse proxy)
и `TELEGRAM_WEBHOOK_SECRET`.

### Настройки бота в @BotFather

- `/setinline` — включить inline-режим (подсказка: `[@username] 150 USDT [сеть] описание`).
- `/setinlinefeedback` — 100 %, чтобы бот мог обновлять отправленные inline-карточки.
- `/setcommands`:
  ```
  new - Новая сделка
  sell - Продать: /sell @username 150 USDT trc20 описание
  buy - Купить: /buy @username 150 USDT описание
  menu - Главное меню
  help - Как это работает
  ```

## Тесты

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user@localhost:5432/escrow_test uv run pytest
uv run ruff check . && uv run mypy app
```

Тесты интеграции и бота работают на настоящем PostgreSQL: блокировки строк, частичные уникальные
индексы и триггеры — часть денежных гарантий. Бот проверяется сквозными тестами: настоящий
диспетчер aiogram и обработчики, вызовы Telegram API записываются в памяти.
