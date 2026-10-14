        kind="видео"
        size=m.video.file_size or 0
    elif m.document and (m.document.mime_type or "").startswith("video/"):
        file_id=m.document.file_id
        kind="видеофайл"
        size=m.document.file_size or 0
    else:
        return
    await m.answer(f"✅ {kind.capitalize()} принят. Добавь этот Telegram file_id в список видео приза (тип выдачи «Видео»). Для нескольких видео вставляй каждый file_id с новой строки или разделяй точкой с запятой.\n\n<code>{file_id}</code>\n\nРазмер: {size/1024/1024:.1f} МБ\nПри выигрыше бот случайно выберет нужное количество видео из списка и отправит их напрямую через Telegram.",parse_mode="HTML")

@dp.callback_query(F.data.startswith("payok:"))
async def bot_approve_payment(callback):
    uid=callback.from_user.id
    if uid not in admin_ids_all():
        await callback.answer("Нет доступа", show_alert=True)
        return
    oid=callback.data.split(":",1)[1]
    with db() as c:
        row=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
        if not row:
            await callback.answer("Заявка не найдена", show_alert=True); return
        if row["status"]!="pending":
            await callback.answer(f"Уже обработана: {row['status']}", show_alert=True); return
        try: info=json.loads(row["details"] or "{}")
        except Exception: info={}
        spins=int(info.get("spins",0))
        if spins not in PACKS:
            await callback.answer("Некорректный пакет", show_alert=True); return
        cur=c.execute("UPDATE orders SET status='approved' WHERE id=? AND status='pending'",(oid,))
        if cur.rowcount != 1:
            await callback.answer("Заявка уже обработана", show_alert=True); return
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(row["telegram_id"],))
        c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?",(spins,row["telegram_id"]))
        if int(info.get("discount_pct", 0) or 0) > 0:
            c.execute("UPDATE users SET spin_discount_pct=0,spin_discount_expires_at=NULL WHERE telegram_id=? AND spin_discount_pct=?", (row["telegram_id"], int(info.get("discount_pct", 0))))
        inviter_id = pay_referral_reward(c, row["telegram_id"])
    audit(uid,"approve_order_bot",oid)
    try: await bot.send_message(row["telegram_id"],f"✅ Оплата подтверждена! Начислено вращений: {spins}. Сумма: {info.get('rub',0)} ₽.")
    except Exception: pass
    await notify_referral_reward(inviter_id, row["telegram_id"])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"✅ Заявка {oid} подтверждена администратором {uid}. Начислено {spins} вращений пользователю {row['telegram_id']}.")
    except Exception: pass
    await callback.answer("Оплата подтверждена, вращения начислены")

@dp.callback_query(F.data.startswith("payno:"))
async def bot_reject_payment(callback):
    uid=callback.from_user.id
    if uid not in admin_ids_all():
        await callback.answer("Нет доступа", show_alert=True)
        return
    oid=callback.data.split(":",1)[1]
    with db() as c:
        row=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
        if not row:
            await callback.answer("Заявка не найдена", show_alert=True); return
        if row["status"]!="pending":
            await callback.answer(f"Уже обработана: {row['status']}", show_alert=True); return
        c.execute("UPDATE orders SET status='rejected' WHERE id=? AND status='pending'",(oid,))
    audit(uid,"reject_order_bot",oid)
    try: await bot.send_message(row["telegram_id"],"❌ Оплата отклонена администратором. Проверь чек и реквизиты; если считаешь это ошибкой, свяжись с администратором.")
    except Exception: pass
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"❌ Заявка {oid} отклонена администратором {uid}. Вращения не начислены.")
    except Exception: pass
    await callback.answer("Заявка отклонена")

@dp.message(Command("paysupport"))
async def paysupport(m: Message):
    await m.answer("По вопросам оплаты напиши владельцу бота и укажи дату и сумму платежа.")

@dp.pre_checkout_query()
async def checkout(q: PreCheckoutQuery):
    # A discounted invoice can be paid only while that exact discount is still active.
    parts = (q.invoice_payload or "").split(":")
    if len(parts) >= 4 and parts[0] == "spinpack":
        try:
            uid = int(parts[1]); discount_pct = int(parts[3])
        except (TypeError, ValueError):
            await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Счёт некорректен. Создай новый счёт в приложении.")
            return
        if uid != q.from_user.id:
            await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Этот счёт создан для другого пользователя.")
            return
        if discount_pct > 0:
            with db() as c:
                user = c.execute("SELECT spin_discount_pct,spin_discount_expires_at FROM users WHERE telegram_id=?", (uid,)).fetchone()
            valid = bool(user and int(user["spin_discount_pct"] or 0) == discount_pct and user["spin_discount_expires_at"])
            if valid:
                try:
                    expires = datetime.fromisoformat(user["spin_discount_expires_at"])
                    if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
                    valid = datetime.now(timezone.utc) < expires.astimezone(timezone.utc)
                except (ValueError, TypeError): valid = False
            if not valid:
                await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Скидка уже использована или истекла. Создай новый счёт без скидки.")
                return
    await bot.answer_pre_checkout_query(q.id,ok=True)

@dp.message(F.successful_payment)
async def paid(m: Message):
    p=m.successful_payment; uid=m.from_user.id
    parts = p.invoice_payload.split(":")
    try:
        if len(parts) < 3 or parts[0] != "spinpack" or int(parts[1]) != uid: raise ValueError("bad payload")
        n = int(parts[2])
        invoice_discount_pct = int(parts[3]) if len(parts) > 3 else 0
        if n not in PACKS: raise ValueError("bad pack")
    except Exception:
        await m.answer("⚠️ Не удалось проверить счёт. Напиши администратору и пришли чек оплаты.")
        return
    with db() as c:
        cur=c.execute("INSERT OR IGNORE INTO payments(charge_id,telegram_id,total_amount,currency,spins_added) VALUES(?,?,?,?,?)",
                      (p.telegram_payment_charge_id,uid,p.total_amount,p.currency,n))
        if cur.rowcount:
            c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(uid,))
            c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?",(n,uid))
            if invoice_discount_pct > 0:
                c.execute("UPDATE users SET spin_discount_pct=0,spin_discount_expires_at=NULL WHERE telegram_id=? AND spin_discount_pct=?", (uid, invoice_discount_pct))
            inviter_id = pay_referral_reward(c, uid)
        else:
            inviter_id = None
    await notify_referral_reward(inviter_id, uid)
    # Баланс обновится в Mini App; пригласивший получит уведомление о бонусе.

async def main():
