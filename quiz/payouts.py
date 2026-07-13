import uuid
import requests
from django.conf import settings
from django.db import models
from django.utils import timezone
from datetime import timedelta
from .models import PaystackTransaction, QuizPool

def execute_automated_pool_payout():
    active_pool_record = QuizPool.objects.filter(is_active=True).order_by('-start_time').first()
    if not active_pool_record:
        return

    total_prize_pot = float(active_pool_record.grand_prize_pool)

    active_transactions = PaystackTransaction.objects.filter(
        status='SUCCESS',
        payout_status='UNPROCESSED',
        game_session__isnull=False,
        game_session__pool=active_pool_record,
        game_session__is_disqualified=False
    )
    
    if not active_transactions.exists():
        active_pool_record.is_active = False
        active_pool_record.save()
        return

    highest_score = active_transactions.aggregate(models.Max('game_session__score'))['game_session__score__max']
    if highest_score is None:
        return

    top_transactions = active_transactions.filter(game_session__score=highest_score)
    contenders = list(top_transactions.order_by('game_session__duration_seconds'))
    if not contenders:
        return

    fastest_time = contenders[0].game_session.duration_seconds
    winners = [tx for tx in contenders if tx.game_session.duration_seconds == fastest_time]

    active_transactions.exclude(paystack_reference__in=[w.paystack_reference for w in winners]).update(payout_status='EVALUATED')

    number_of_winners = len(winners)
    payout_per_winner = total_prize_pot / number_of_winners

    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    if payout_per_winner > 0:
        for winner in winners:
            winner.payout_status = 'PENDING'
            winner.save()
            
            phone = winner.phone_number.strip()
            
            recipient_payload = {
                "type": "mobile_money",
                "name": winner.player_name or "PesaQuiz Player",
                "account_number": phone,
                "bank_code": "MPESA",
                "currency": "KES"
            }
            
            try:
                rec_res = requests.post("https://api.paystack.co/transferrecipient", json=recipient_payload, headers=headers).json()
                if not rec_res.get('status'):
                    winner.payout_status = 'FAILED'
                    winner.save()
                    continue
                    
                recipient_code = rec_res['data']['recipient_code']
                
                transfer_payload = {
                    "source": "balance",
                    "amount": int(payout_per_winner * 100),
                    "recipient": recipient_code,
                    "reference": str(uuid.uuid4()),
                    "reason": f"Pool #{active_pool_record.id} - Score: {highest_score}"
                }
                
                trans_res = requests.post("https://api.paystack.co/transfer", json=transfer_payload, headers=headers).json()
                
                if trans_res.get('status') and trans_res['data']['status'] in ['success', 'pending']:
                    winner.payout_status = 'PAID'
                else:
                    winner.payout_status = 'FAILED'
                winner.save()

            except requests.exceptions.RequestException:
                winner.payout_status = 'FAILED'
                winner.save()

    active_pool_record.is_active = False
    active_pool_record.save()
    
    now = timezone.now()
    QuizPool.objects.create(
        start_time=now,
        end_time=now + timedelta(hours=3),
        is_active=True
    )