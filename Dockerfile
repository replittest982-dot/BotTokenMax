
# 1. Используем официальный образ Playwright
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# 2. Устанавливаем рабочую директорию
WORKDIR /app

# 3. Указываем переменную окружения, чтобы Playwright ставил браузеры в /app/pw-browsers
# Это исключит ошибки доступа к папке /root
ENV PLAYWRIGHT_BROWSERS_PATH=/app/pw-browsers

# 4. Копируем requirements и ставим либы
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Устанавливаем ТОЛЬКО chromium (чтобы не раздувать образ)
RUN playwright install chromium

# 6. Копируем остальной код
COPY . .

# 7. Запуск
CMD ["python", "main.py"]
