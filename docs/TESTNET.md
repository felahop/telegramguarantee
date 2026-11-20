# Запуск в testnet

Цель: пройти полный цикл сделки на тестовых деньгах — создание, оплата, подтверждения, выплата,
спор, возврат — до того, как бот увидит настоящие USDT.

## 1. Окружение

```bash
uv sync
cp .env.example .env
cp chains.example.yaml chains.yaml
docker compose up -d postgres redis   # или свои PostgreSQL 16 и Redis 7
uv run alembic upgrade head
```

В `.env`:

| Переменная | Значение для testnet |
|---|---|
| `ENVIRONMENT` | `dev` (не `prod`: в проде testnet-режим запрещён) |
| `BOT_TOKEN`, `BOT_USERNAME` | отдельный тестовый бот из @BotFather |
| `ADMIN_IDS` | ваш Telegram ID |
| `SECRETS_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `CRYPTOBOT_TESTNET` | `true` |
| `TESTNET_MODE` | `true`, только если нужны testnet-адреса TON |
| `DEAL_MAX_USDT`, `SIGNER_MAX_PAYOUT_USDT`, `SIGNER_DAILY_LIMIT_USDT` | маленькие значения: так проверяется срабатывание лимитов |

## 2. CryptoBot (самый быстрый путь)

1. @CryptoTestnetBot → Crypto Pay → Create App → токен в `CRYPTOBOT_API_TOKEN`.
2. В настройках приложения включите **Transfers**. Webhook нужен только при запуске через `web`
   (polling-воркер найдёт оплату и без него).
3. Тестовые монеты: @CryptoTestnetBot выдаёт их в разделе кошелька.
4. Запуск: `uv run python -m app.bot` (long polling) и `uv run python -m app.worker`.

Сценарий проверки: `/buy @второй_аккаунт 10 USDT cb тест` → второй аккаунт принимает →
«Оплатить» → оплата в @CryptoTestnetBot → «Я выполнил» → «Получил» → выплата приходит в
@CryptoTestnetBot продавца. Затем повторите со спором: «Открыть спор» → `/admin` → решение
с кодом TOTP.

## 3. Ончейн-сети

В `chains.yaml` у каждой тестовой сети замените контракт на **тестовый токен** (официального USDT в
testnet нет). Проще всего развернуть свой ERC-20 / TRC-20 с нужными `decimals` и указать его
`contract` и `decimals`. Воркер при старте сверит `decimals` с контрактом и откажется запускаться
при расхождении.

| Сеть | `chain_id` | Пример `rpc` | Пример `verify_rpc` (другой провайдер!) |
|---|---|---|---|
| BSC testnet | 97 | `https://bsc-testnet-rpc.publicnode.com` | `https://data-seed-prebsc-1-s1.bnbchain.org:8545` |
| Ethereum Sepolia | 11155111 | `https://ethereum-sepolia-rpc.publicnode.com` | `https://rpc.sepolia.org` |
| TRON Nile | — | `https://nile.trongrid.io` | другой узел Nile |
| Solana devnet | — | `https://api.devnet.solana.com` | другой провайдер devnet |

Перед использованием проверьте эндпоинты: публичные RPC меняются, режут `eth_getLogs` и
ограничивают частоту запросов.

Горячий кошелёк — это `deposit_address`:

```bash
mkdir -p secrets/keystore
uv run python -m app.cli keystore-import secrets/keystore/hot.json   # ключ и пароль вводятся скрыто
```

В `.env`: `SIGNER_KEYSTORE_DIR=secrets/keystore` и `SIGNER_PASSWORD=…`. На кошелёк положите тестовые
токены и монету для газа (tBNB, SepoliaETH, Nile TRX). Пополнение газа запишите в ledger:
`uv run python -m app.cli record-funding BNB@bsc <адрес> 0.1 faucet-1`.

Проверьте отдельно:

- точная сумма с меткой → «Оплачено»; сумма на 1 меньше → «Неопознанные» в админке;
- «Бот не видит оплату — указать hash» на уже зачисленной транзакции → «уже зачислена»;
- выплата: `signing` → `broadcast` → `confirmed`, сделка «Завершена»;
- `uv run python -m app.cli reconcile` → `diff 0.00` по каждому кошельку;
- остановка выплат в «Балансы» → выплаты ждут; «Возобновить» с TOTP → уходят.

## 4. Отдельный подписант (как в проде)

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # → SIGNER_API_SECRET в .env
docker compose up -d --build
```

`signer` находится только во внутренней сети без выхода в интернет, а воркер обращается к нему по
`SIGNER_URL=http://signer:8090`. Проверка: в логах воркера нет `signer_in_process`, а
`docker compose exec signer python -c "import urllib.request; urllib.request.urlopen('https://example.com')"`
падает: у подписанта нет интернета.

## 5. Перед переходом на mainnet

Пройдите [docs/RISKS.md](RISKS.md) и закройте все пункты из раздела «Блокирующие».
