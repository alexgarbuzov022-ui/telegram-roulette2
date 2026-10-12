# Telegram Roulette upgrade
1. Replace `main.py`, `web/index.html`, and `requirements.txt` in the GitHub repo with these files.
2. In Render Environment set:
   - BOT_TOKEN = your current BotFather token (never share it)
   - WEBAPP_URL = your actual Render service URL
   - ADMIN_IDS = your Telegram numeric user ID (comma-separated IDs if multiple owners)
   - SPIN_PRICE_STARS = 50
   - DB_PATH = roulette.sqlite3
3. Commit the changes and wait for Render deploy.
4. Important: free Render local filesystem/SQLite may be reset on restarts or redeploys. For real payments and persistent inventory use a persistent disk or managed PostgreSQL.
5. This starter implements Stars payments for spins. It does NOT implement external card/crypto checkout for digital goods; Telegram rules require Stars for digital goods sold inside Telegram. Do not use external payments to bypass that rule.
6. Initial prizes are demo virtual points. Admin UI currently supports prize name/description/image/rarity/weight/points, upload or URL image, and disable. A secure real prize inventory/code distribution system and full manual card/crypto request workflow need a separate, policy-compliant implementation.
