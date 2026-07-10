# quiz/payouts.py
import uuid
import requests
from decimal import Decimal
from django.conf import settings
from django.db import models
from .models import PaystackTransaction, QuizPool

def execute_automated_pool_payout():
    """
    Closes the active 3-hour pool dynamically based on actual entry collections.
    Finds the top scorers, handles ties, and triggers Paystack M-Pesa payouts.
    """
    # 1. Locate the currently active pool tracking record
    active_pool_record = QuizPool.objects.filter(is_active=True).order_by('-start_time').first()
    
    if not active_pool_record:
        print("ℹ️ No active quiz pool ledger records found.")
        return

    # 💵 DYNAMIC CALCULATION: Read the exact amount collected in this pool
    total_prize_pot = float(active_pool_record.grand_prize_pool)
    print(f"💰 Dynamic Pool Closure Triggered. Accumulated Prize Pot: KES {total_prize_pot}")

    # 2. Gather successful, unprocessed game transactions linked to this active pool
    # (Matches transaction sessions where the player wasn't disqualified)
    active_transactions = PaystackTransaction.objects.filter(
        status='SUCCESS',
        payout_status='UNPROCESSED',
        game_session__isnull=False,
        game_session__pool=active_pool_record,
        game_session__is_disqualified=False
    )
    
    if not active_transactions.exists():
        print("ℹ️ No active game transactions found in this pool window. Resetting pool.")
        # Close the empty pool and open a new window anyway
        active_pool_record.is_active = False
        active_pool_record.save()
        return

    # 3. Identify highest score inside this pool window
    highest_score = active_transactions.aggregate(models.Max('game_session__score'))['game_session__score__max']
    if highest_score is None:
        return

    top_transactions = active_transactions.filter(game_session__score=highest_score)
    
    # Sort by fastest time_taken (duration_seconds)
    contenders = list(top_transactions.order_by('game_session__duration_seconds'))
    if not contenders:
        return

    fastest_time = contenders[0].game_session.duration_seconds
    winners = [tx for tx in contenders if tx.game_session.duration_seconds == fastest_time]

    # 🔒 CLOSE POOL FOR LOSERS: Mark them evaluated so they don't leak into the next cycle
    active_transactions.exclude(paystack_reference__in=[w.paystack_reference for w in winners]).update(payout_status='EVALUATED')

    # 4. Handle Split Pot logic dynamically if an absolute tie occurs
    number_of_winners = len(winners)
    payout_per_winner = total_prize_pot / number_of_winners

    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    # 5. Process payouts if the pool actually generated money
    if payout_per_winner > 0:
        for winner in winners:
            winner.payout_status = 'PENDING'
            winner.save()
            
            phone = winner.phone_number.strip()
            
            # Phase A: Generate Transfer Recipient
            recipient_url = "https://api.paystack.co/transferrecipient"
            recipient_payload = {
                "type": "mobile_money",
                "name": winner.player_name or "PesaQuiz Player",
                "account_number": phone,
                "bank_code": "MPESA",
                "currency": "KES"
            }
            
            try:
                rec_res = requests.post(recipient_url, json=recipient_payload, headers=headers).json()
                if not rec_res.get('status'):
                    winner.payout_status = 'FAILED'
                    winner.save()
                    continue
                    
                recipient_code = rec_res['data']['recipient_code']
                
                # Phase B: Dispatch automated M-Pesa transfer
                transfer_url = "https://api.paystack.co/transfer"
                transfer_payload = {
                    "source": "balance",
                    "amount": int(payout_per_winner * 100),  # Minor units (cents)
                    "recipient": recipient_code,
                    "reference": str(uuid.uuid4()),
                    "reason": f"PesaQuiz Winner Pool #{active_pool_record.id} - Score: {highest_score}"
                }
                
                trans_res = requests.post(transfer_url, json=transfer_payload, headers=headers).json()
                
                if trans_res.get('status') and trans_res['data']['status'] in ['success', 'pending']:
                    winner.payout_status = 'PAID'
                else:
                    winner.payout_status = 'FAILED'
                winner.save()

            except requests.exceptions.RequestException:
                winner.payout_status = 'FAILED'
                winner.save()

    # 🔄 CYCLE RESET: Mark the current database pool closed
    active_pool_record.is_active = False
    active_pool_record.save()
    
    # Automatically spawn a new clean database pool record for the next 3 hours
    from django.utils import timezone
    from datetime import timedelta
    
    now = timezone.now()
    QuizPool.objects.create(
        start_time=now,
        end_time=now + timedelta(hours=3),
        is_active=True
    )
    print("🚀 New pool opened successfully! Ready for the next repeating batch.")