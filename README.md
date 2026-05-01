# Flúx — сервис для копирования сайтов

## Быстрый старт

```bash
# 1. Установить зависимости
pip install -r requirements.txt

# 2. Запустить сервер
python app.py

# 3. Открыть в браузере
http://localhost:5000
```

## Структура проекта

```
Flúx
|──  README.md
├── requirements.txt  ← Зависимости Python
└── static/
         ├── index.html
         ├── perplex.css
         ├── up-icon.svg
         └── fonts/
                  ├── CabinetGrotesk-Black.woff2
                  └── instrument-serif-v5-latin-regular.woff2

```

## API

### POST /api/copy

Тело запроса (JSON):

| Поле              | Тип     | Описание                              |
|-------------------|---------|---------------------------------------|
| `url`             | string  | Адрес сайта для копирования           |
| `rename_files`    | boolean | Переименовать файлы в UUID (optional) |
| `mobile_version`  | boolean | Использовать mobile user-agent        |
| `crawl_all_pages` | boolean | Обходить все внутренние страницы      |

Ответ: ZIP-архив (application/zip) либо JSON с полем `error`.

## Ограничения

- Максимум **200 файлов** за один запрос
- Максимальный размер файла: **10 MB**
- Таймаут на каждый запрос: **15 секунд**
- Сайты с авторизацией/капчей копируются частично
