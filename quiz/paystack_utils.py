import logging
import hmac
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone
from django.core.management import call_command

from .models import (
    QuizPool, 
    PaystackTransaction, 
    PayoutEvent, 
    StandbyQuestion, 
    QuestionBank,
    normalize_kenyan_phone
)
from .tasks import send_victory_sms

logger = logging.getLogger(__name__)


def verify_paystack_signature(payload_body, signature_header):
    secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    if not secret_key or not signature_header:
        logger.error("Paystack secret key or signature header missing.")
        return False
        
    computed_signature = hmac.new(
        secret_key.encode('utf-8'),
        payload_body,
        hashlib.sha512
    ).hexdigest()
    
    return hmac.compare_digest(computed_signature, signature_header)


def verify_paystack_transaction(reference):
    secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', None)
    if not secret_key:
        logger.error("PAYSTACK_SECRET_KEY is missing from settings.")
        return None

    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            response_data = response.json()
            if response_data.get('status') and response_data.get('data', {}).get('status') == 'success':
                data = response_data['data']
                
                amount_minor = Decimal(str(data.get('amount', 0)))
                amount_kes = amount_minor / Decimal('100.00')
                
                fee_minor = Decimal(str(data.get('fees', 0)))
                fee_kes = fee_minor / Decimal('100.00')
                
                return {
                    'status': 'SUCCESS',
                    'amount': amount_kes,
                    'fee': fee_kes,
                    'net_amount': amount_kes - fee_kes,
                    'email': data.get('customer', {}).get('email'),
                    'metadata': data.get('metadata'),
                }
        return {'status': 'FAILED', 'amount': Decimal('0.00')}
    except requests.RequestException as e:
        logger.error(f"Paystack API network error for ref {reference}: {e}")
        return None


def start_next_pool():
    with transaction.atomic():
        QuizPool.objects.filter(is_active=True).update(is_active=False)
        
        now = timezone.now()
        new_pool = QuizPool.objects.create(
            start_time=now,
            end_time=now + timedelta(hours=3),
            is_active=True,
            grand_prize_pool=Decimal('0.00')
        )
        
        try:
            call_command('seed_quiz_questions')
        except Exception as e:
            logger.error(f"Failed to seed questions for pool {new_pool.id}: {e}")
            
        return new_pool


def create_transfer_recipient(name, phone_number):
    url = "https://api.paystack.co/transferrecipient"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    
    # Normalize phone format before API dispatch
    try:
        formatted_phone = normalize_kenyan_phone(phone_number)
    except Exception:
        formatted_phone = phone_number

    payload = {
        "type": "mobile_money",
        "name": name or "PesaQuiz Winner",
        "account_number": formatted_phone,
        "currency": "KES",
        "settlement_bank": "MPESA"
    }
    
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        res_data = res.json()
        if res.status_code in (200, 201) and res_data.get('status'):
            return res_data['data']['recipient_code']
        logger.error(f"Recipient creation failed for {formatted_phone}: {res_data.get('message')}")
        return None
    except Exception as e:
        logger.error(f"Transfer recipient API error: {e}")
        return None


def send_paystack_transfer(recipient_code, amount, reason="PesaQuiz Prize Payout"):
    url = "https://api.paystack.co/transfer"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    
    amount_in_cents = int(Decimal(str(amount)) * 100)
    
    payload = {
        "source": "balance",
        "reason": reason,
        "amount": amount_in_cents,
        "recipient": recipient_code
    }
    
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        res_data = res.json()
        if res.status_code in (200, 201) and res_data.get('status'):
            return True, res_data.get('data', {})
        return False, res_data.get('message', 'Transfer failed')
    except Exception as e:
        logger.error(f"Paystack transfer API error: {e}")
        return False, str(e)


def execute_automated_pool_payout():
    winners = []
    payout_per_winner = Decimal('0.00')
    active_pool = None

    # Step 1: Identify winners under database row lock
    with transaction.atomic():
        active_pool = QuizPool.objects.select_for_update().filter(is_active=True).first()
        if not active_pool:
            logger.info("No active pool found to process payouts.")
            return

        eligible_txs = PaystackTransaction.objects.select_for_update().filter(
            pool=active_pool,
            status='SUCCESS',
            payout_status='UNPROCESSED',
            game_session__isnull=False,
            game_session__is_disqualified=False
        )

        if eligible_txs.exists():
            top_score = eligible_txs.aggregate(Max('game_session__score'))['game_session__score__max']
            
            if top_score is not None:
                contenders = eligible_txs.filter(game_session__score=top_score)
                best_time = contenders.aggregate(Min('game_session__duration_seconds'))['game_session__duration_seconds__min']
                
                winners = list(contenders.filter(game_session__duration_seconds=best_time))
                
                if winners and active_pool.grand_prize_pool > 0:
                    raw_share = active_pool.grand_prize_pool / Decimal(len(winners))
                    payout_per_winner = raw_share.quantize(Decimal('0.01'), rounding=ROUND_DOWN)

        # Deactivate current pool so no further entries register under it
        active_pool.is_active = False
        active_pool.save()

    # Step 2: Execute Paystack Transfers, Log Audit Events, & Trigger Victory SMS (Outside DB lock)
    if winners and payout_per_winner > Decimal('0.00'):
        for tx in winners:
            recipient_code = create_transfer_recipient(tx.player_name, tx.phone_number)
            if not recipient_code:
                tx.payout_status = 'FAILED'
                tx.save()
                continue

            success, response_data = send_paystack_transfer(
                recipient_code=recipient_code,
                amount=payout_per_winner,
                reason=f"PesaQuiz Win Pool #{active_pool.id}"
            )

            if success:
                tx.payout_status = 'PAID'
                tx.payout_amount = payout_per_winner
                tx.transfer_code = response_data.get('transfer_code', '')
                tx.save()

                # 1. Log immutable audit trail in PayoutEvent
                try:
                    if hasattr(tx, 'game_session'):
                        PayoutEvent.objects.create(
                            transaction_receipt=tx,
                            pool=active_pool,
                            game_session=tx.game_session,
                            amount=payout_per_winner,
                            status='SUCCESS'
                        )
                except Exception as audit_err:
                    logger.error(f"Failed to record PayoutEvent for TX {tx.paystack_reference}: {audit_err}")

                # 2. Trigger Celery victory SMS notification
                try:
                    score = tx.game_session.score if hasattr(tx, 'game_session') else 0
                    send_victory_sms.delay(
                        normalized_phone=tx.phone_number,
                        player_name=tx.player_name or "Winner",
                        score=score,
                        prize_share_str=str(payout_per_winner)
                    )
                except Exception as sms_err:
                    logger.error(f"Failed to dispatch Celery SMS task for {tx.phone_number}: {sms_err}")

            else:
                tx.payout_status = 'FAILED'
                tx.save()

    # Step 3: Spin up the new active pool cycle
    start_next_pool()