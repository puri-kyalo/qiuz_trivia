import uuid
import logging
import requests
from decimal import Decimal, ROUND_DOWN
from django.conf import settings
from django.db import transaction, models
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command  # <-- For seeding the new pool automatically
from .models import PaystackTransaction, QuizPool

logger = logging.getLogger(__name__)

def execute_automated_pool_payout():
    """
    Evaluates the active pool, identifies highest scoring players (with time tiebreakers),
    distributes payouts via Paystack Mobile Money (M-Pesa), and starts a fresh pool.
    """
    winners = []
    payout_per_winner = Decimal('0.00')
    highest_score = 0

    # Use select_for_update inside an atomic block to prevent double-payout race conditions
    with transaction.atomic():
        active_pool = QuizPool.objects.select_for_update().filter(is_active=True).order_by('-start_time').first()
        if not active_pool:
            logger.warning("No active pool found to process.")
            return

        # Keep everything in safe Decimal format for currency calculations
        total_prize_pot = Decimal(str(active_pool.grand_prize_pool))

        active_transactions = PaystackTransaction.objects.filter(
            status='SUCCESS',
            payout_status='UNPROCESSED',
            game_session__isnull=False,
            game_session__pool=active_pool,
            game_session__is_disqualified=False
        )
        
        if not active_transactions.exists():
            active_pool.is_active = False
            active_pool.save()
            
            # Start the next pool immediately so players can continue
            start_next_pool()
            return

        # Locate the highest score in this pool
        highest_score = active_transactions.aggregate(models.Max('game_session__score'))['game_session__score__max']
        if highest_score is None:
            active_pool.is_active = False
            active_pool.save()
            start_next_pool()
            return

        # Tie-breaker logic: select fastest duration among players with the highest score
        top_scorers = active_transactions.filter(game_session__score=highest_score)
        contenders = list(top_scorers.order_by('game_session__duration_seconds'))
        if not contenders:
            active_pool.is_active = False
            active_pool.save()
            start_next_pool()
            return

        fastest_time = contenders[0].game_session.duration_seconds
        winners = [tx for tx in contenders if tx.game_session.duration_seconds == fastest_time]

        # Update players who didn't win
        active_transactions.exclude(paystack_reference__in=[w.paystack_reference for w in winners]).update(payout_status='EVALUATED')

        number_of_winners = len(winners)
        if number_of_winners > 0:
            payout_per_winner = (total_prize_pot / Decimal(str(number_of_winners))).quantize(Decimal('0.01'), rounding=ROUND_DOWN)

    # Trigger API payouts outside of the DB lock transaction block to prevent long-running table locks
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    if payout_per_winner > 0 and winners:
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
                rec_res = requests.post(
                    "https://api.paystack.co/transferrecipient", 
                    json=recipient_payload, 
                    headers=headers
                ).json()
                
                if not rec_res.get('status'):
                    winner.payout_status = 'FAILED'
                    winner.save()
                    continue
                    
                recipient_code = rec_res['data']['recipient_code']
                
                # Paystack expects amounts in cents/pesewas (multiply by 100 as integer)
                amount_cents = int(payout_per_winner * 100)
                
                transfer_payload = {
                    "source": "balance",
                    "amount": amount_cents,
                    "recipient": recipient_code,
                    "reference": str(uuid.uuid4()),
                    "reason": f"PesaQuiz Pool #{active_pool.id} - Score: {highest_score}"
                }
                
                trans_res = requests.post(
                    "https://api.paystack.co/transfer", 
                    json=transfer_payload, 
                    headers=headers
                ).json()
                
                if trans_res.get('status') and trans_res.get('data', {}).get('status') in ['success', 'pending']:
                    winner.payout_status = 'PAID'
                else:
                    winner.payout_status = 'FAILED'
                winner.save()

            except requests.exceptions.RequestException as e:
                logger.error(f"Paystack request failed for transaction {winner.paystack_reference}: {str(e)}")
                winner.payout_status = 'FAILED'
                winner.save()

    # Finally, deactivate old pool and create & seed the next one
    with transaction.atomic():
        active_pool.is_active = False
        active_pool.save()
        
    start_next_pool()


def start_next_pool():
    """
    Creates the next 3-hour quiz pool and runs the seeding command to 
    guarantee that fresh trivia questions are generated immediately.
    """
    now = timezone.now()
    new_pool = QuizPool.objects.create(
        start_time=now,
        end_time=now + timedelta(hours=3),
        is_active=True
    )
    
    # Run our seeding script so players don't hit an empty question bank!
    try:
        logger.info(f"Automatically seeding questions for newly created Pool #{new_pool.id}")
        call_command('seed_quiz_questions')
    except Exception as e:
        logger.error(f"Failed to automatically seed new pool: {str(e)}")