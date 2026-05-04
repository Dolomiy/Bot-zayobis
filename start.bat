@echo off
chcp 65001 >nul
title Бот повернень

:: Перевірка наявності Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ПОМИЛКА] Python не знайдено. Встановіть Python 3.11+ з https://python.org
    pause
    exit /b 1
)

:: Перевірка наявності .env
if not exist ".env" (
    if exist ".env.example" (
        echo [УВАГА] Файл .env не знайдено.
        echo Копіюю .env.example -^> .env
        copy ".env.example" ".env" >nul
        echo Відредагуйте .env і запустіть знову.
        start notepad ".env"
        pause
        exit /b 1
    ) else (
        echo [ПОМИЛКА] Файл .env не знайдено. Створіть його за прикладом .env.example
        pause
        exit /b 1
    )
)

:: Створення віртуального середовища якщо відсутнє
if not exist "venv\Scripts\activate.bat" (
    echo [INFO] Створення віртуального середовища...
    python -m venv venv
    if errorlevel 1 (
        echo [ПОМИЛКА] Не вдалося створити venv
        pause
        exit /b 1
    )
)

:: Активація venv
call venv\Scripts\activate.bat

:: Оновлення pip та встановлення залежностей
echo [INFO] Перевірка залежностей...
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ПОМИЛКА] Не вдалося встановити залежності
    pause
    exit /b 1
)

:: Запуск бота
echo [INFO] Запуск бота... (Ctrl+C для зупинки)
echo.
python bot.py

:: Якщо бот завершився з помилкою — не закривати вікно одразу
if errorlevel 1 (
    echo.
    echo [!] Бот завершив роботу з помилкою. Перевірте лог вище або файл bot.log
    pause
)
