import logging
import time
from decimal import Decimal, ROUND_DOWN
from functools import partial
from django.core.management.base import BaseCommand
from django.db import transaction, models
from django.utils import timezone
from quiz.models import QuizPool, GameSession, PayoutEvent, PlatformRevenueAccount

try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Rotates quiz pools and processes payouts"

    def handle(self, *args, **options):
        now = timezone.now()

        with transaction.atomic():
            current_pool = QuizPool.objects.select_for_update().filter(is_active=True).first()
            
            if not current_pool:
                current_pool = QuizPool.objects.create(
                    start_time=now,
                    end_time=now + timezone.timedelta(hours=3),
                    is_active=True,
                    grand_prize_pool=Decimal('0.00'),
                    total_entries=0
                )

            current_pool.is_active = False
            current_pool.save()

            next_pool = QuizPool.objects.create(
                start_time=current_pool.end_time,
                end_time=current_pool.end_time + timezone.timedelta(hours=3),
                is_active=True,
                grand_prize_pool=Decimal('0.00'),
                total_entries=0
            )

        from quiz.tasks import seed_questions_for_pool
        seed_questions_for_pool.delay(next_pool.id)

        time.sleep(10)

        if current_pool.total_entries == 0:
            return

        expected_prize_pool = Decimal(str(current_pool.total_entries)) * Decimal('10.00')
        if current_pool.grand_prize_pool != expected_prize_pool:
            error_msg = f"Pool mismatch {current_pool.id}: Expected {expected_prize_pool}, found {current_pool.grand_prize_pool}"
            logger.critical(error_msg)
            if sentry_sdk:
                sentry_sdk.capture_message(error_msg, level="critical")
            
            current_pool.requires_manual_audit = True
            current_pool.save()
            return 

        valid_sessions = GameSession.objects.filter(
            pool=current_pool,
            is_disqualified=False,
            end_time__isnull=False
        ).select_related('transaction_reference').order_by('-score', 'duration_seconds')

        if not valid_sessions.exists():
            return

        winning_score = valid_sessions.first().score
        winners = [session for session in valid_sessions if session.score == winning_score]
        winner_count = len(winners)

        total_distributable_funds = current_pool.grand_prize_pool
        raw_prize_share = (total_distributable_funds / Decimal(str(winner_count))).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        
        total_allocated_winnings = raw_prize_share * Decimal(str(winner_count))
        fractional_remainder = total_distributable_funds - total_allocated_winnings

        with transaction.atomic():
            if fractional_remainder > 0:
                PlatformRevenueAccount.objects.get_or_create(
                    id=1, 
                    defaults={'account_name': 'Platform Revenue', 'balance': Decimal('0.00')}
                )
                admin_account = PlatformRevenueAccount.objects.select_for_update().get(id=1)
                admin_account.balance = models.F('balance') + fractional_remainder
                admin_account.save()

            from quiz.tasks import send_victory_sms

            for session in winners:
                tx_receipt = session.transaction_reference

                if not tx_receipt:
                    continue

                tx_receipt.payout_status = 'PAID'
                tx_receipt.save()

                PayoutEvent.objects.create(
                    transaction_receipt=tx_receipt,
                    pool=current_pool,
                    game_session=session,
                    amount=raw_prize_share,
                    status='SUCCESS'
                )

                transaction.on_commit(
                    partial(
                        send_victory_sms.delay,
                        tx_receipt.phone_number,
                        tx_receipt.player_name or "Valued Player",
                        session.score,
                        str(raw_prize_share)
                    )
                )