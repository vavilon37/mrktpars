# Окружение для bothost.ru (Custom / Docker).
# curl_cffi и aiogram ставятся из готовых wheel — сборочные тулзы не нужны.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Запускаем именно бота (фоновый цикл сканера крутится внутри него).
CMD ["python", "bot.py"]
