# Escrow-бот «Гарант» для P2P-сделок в USDT

Telegram-бот удерживает оплату покупателя и выплачивает её продавцу только после подтверждения
обеих сторон. Архитектура и решения — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

> Проект в разработке. Готово: схема БД с защитными триггерами, ledger, комиссии, автомат сделок,
> создание сделки в боте (мастер, `/sell` `/buy` `/deal`, inline-режим, приглашения, шаблоны),
> уведомления сторонам, таймеры, **оплата и выплата через CryptoBot**, весь путь сделки в боте
> («Я выполнил», «Получил», отмена по согласию, возврат, споры, чат сделки).
> Ещё нет: админка (решение споров, подтверждение крупных выплат), ончейн-сети.
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

Фоновые задачи (уведомления, таймеры, опрос CryptoBot, выплаты): `uv run python -m app.worker`.

Продакшен-режим — вебхук: `docker compose up -d` поднимает `postgres`, `redis`, `migrate`, `web`
(порт 8080, пути `/tg/webhook` и `/cryptopay/<secret>`) и `worker`. Нужны `WEBHOOK_BASE_URL`
(публичный HTTPS через reverse proxy) и `TELEGRAM_WEBHOOK_SECRET`.

### CryptoBot (Crypto Pay)

1. Testnet: откройте @CryptoTestnetBot → Crypto Pay → Create App. Mainnet — то же в @CryptoBot.
2. Токен приложения → `CRYPTOBOT_API_TOKEN`, `CRYPTOBOT_TESTNET=true|false`.
3. В настройках приложения **включите Transfers** — без этого выплаты продавцам не пройдут.
4. Webhooks → URL `https://<ваш домен>/cryptopay/<CRYPTOBOT_WEBHOOK_SECRET_PATH>` (случайная строка).
   Подпись каждого запроса проверяется; если вебхук потеряется, воркер найдёт оплату опросом.
   Crypto Pay повторяет неуспешную доставку до 17 раз за 3 дня, а потом **отключает вебхуки** —
   после долгого простоя проверьте, что они снова включены (оплаты всё равно найдёт опрос).
5. Получатель выплаты должен хотя бы раз открыть @CryptoBot (@CryptoTestnetBot) — иначе бот
   попросит его об этом и повторит выплату автоматически.
6. Проверьте текущую комиссию Crypto Pay за приём оплаты: если она удерживается из счёта, она
   учитывается как расход сервиса, а минимальная комиссия сервиса должна её покрывать.

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
