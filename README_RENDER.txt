TELEGRAM ROULETTE — RENDER

Файлы уже подготовлены для Render и Python 3.12.

В GitHub загрузите содержимое этой папки в корень репозитория (не саму папку).
НЕ загружайте .env или базу данных.

Render settings:
Build Command: pip install -r requirements.txt
Start Command: python main.py
Instance: Free

В Render -> Environment добавьте:
BOT_TOKEN = новый токен от @BotFather (старый токен был опубликован — обязательно отзовите его через /revoke)
WEBAPP_URL = адрес вашего Render-сервиса, например https://ИМЯ.onrender.com
SPIN_PRICE_STARS = 10
SPINS_PER_PACK = 1
DB_PATH = roulette.sqlite3

Сначала создайте сервис, дождитесь адреса *.onrender.com, затем внесите этот адрес в WEBAPP_URL и сделайте Manual Deploy.
Затем в @BotFather используйте /setmenubutton и укажите этот же HTTPS-адрес.

ВАЖНО:
- Render Free может засыпать после простоя и запускать сервис заново.
- Файл SQLite на бесплатном Render не является надёжным постоянным хранилищем; данные могут потеряться при перезапуске/пересоздании сервиса.
- Не загружайте токен в GitHub и не присылайте его в чат.
