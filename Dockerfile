# Используем образ со всеми системными зависимостями
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Устанавливаем рабочую папку
WORKDIR /app

# Указываем, куда скачивать браузер (важно для прав доступа на хостинге)
ENV PLAYWRIGHT_BROWSERS_PATH=/app/pw-browsers

# Сначала копируем зависимости и ставим их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Скачиваем только Chromium (чтобы образ не весил 2ГБ)
RUN playwright install chromium

# Копируем весь остальной код бота
COPY . .

# Запуск
CMD ["python", "main.py"]
