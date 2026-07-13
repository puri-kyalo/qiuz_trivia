import logging
import time
from decimal import Decimal, ROUND_DOWN
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
    help = "Closes the current active tournament pool, boots up the next window, and handles high-precision wallet settlements."

    def handle(self, *args, **options):
        logger.info("Initiating recurrent 3-hour tournament lifecycle loop execution sequence...")
        now = timezone.now()

        # --- PHASE 1: THE ROTATION BLOCK & ASYNC BUFFER ---
        with transaction.atomic():
            current_pool = QuizPool.objects.select_for_update().filter(is_active=True).first()
            
            if not current_pool:
                logger.warning("Operational anomaly encountered: No active tournament window resolved. Seeding fresh baseline pool.")
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
            logger.info(f"Tournament pool flipped successfully. Active pointer shifted to Pool ID: {next_pool.id}")

        # --- PHASE 2: ASYNC PRE-SEEDING INVOCATION TRIGGER ---
        # Dispatches your background worker task to seed your QuestionBank model arrays for the new pool window
        from quiz.tasks import seed_questions_for_pool
        seed_questions_for_pool.delay(next_pool.id)
        logger.info(f"Decoupled background worker task dispatched for upcoming AI question provisioning on Pool ID: {next_pool.id}")

        # --- PHASE 2.5: DELAYED SETTLEMENT GATEWAY GRACE WINDOW ---
        # Yields execution thread briefly to capture late-arriving payment webhooks from M-Pesa/Paystack network lags
        logger.info("Yielding execution thread briefly for late-arriving payment webhooks...")
        time.sleep(10)

        # --- PHASE 3: ISOLATION AUDITING & HARDENED FILTER SANITIZATION ---
        if current_pool.total_entries == 0:
            logger.info(f"Low Activity Pool Closure encountered for Pool ID: {current_pool.id}. Exiting cleanly.")
            return

        expected_prize_pool = Decimal(str(current_pool.total_entries)) * Decimal('10.00')
        if current_pool.grand_prize_pool != expected_prize_pool:
            error_msg = (
                f"CRITICAL BALANCING VARIANCE DETECTED in Pool ID {current_pool.id}. "
                f"Expected: KSh {expected_prize_pool}, Found: KSh {current_pool.grand_prize_pool}. "
                f"Executing operational isolation protocol."
            )
            logger.critical(error_msg)
            if sentry_sdk:
                sentry_sdk.capture_message(error_msg, level="critical")
            
            current_pool.requires_manual_audit = True
            current_pool.save()
            return 

        # --- PHASE 4: HIGH-PERFORMANCE LEADERBOARD EXTRACTION ---
        valid_sessions = GameSession.objects.filter(
            pool=current_pool,
            is_disqualified=False,
            end_time__isnull=False
        ).select_related('transaction_reference').order_by('-score', 'duration_seconds')

        if not valid_sessions.exists():
            logger.info(f"No valid non-disqualified submissions found for Pool {current_pool.id}. Processing rollover settings.")
            return

        winning_score = valid_sessions.first().score
        
        winners = [
            session for session in valid_sessions 
            if session.score == winning_score
        ]
        
        winner_count = len(winners)
        logger.info(f"Resolved {winner_count} tournament tier-1 winner(s) with Score: {winning_score} points.")

        # --- PHASE 5: ATOMIC BALANCES & EXACT TIE-SPLITTING LEDGERS ---
        total_distributable_funds = current_pool.grand_prize_pool
        raw_prize_share = (total_distributable_funds / Decimal(str(winner_count))).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        
        total_allocated_winnings = raw_prize_share * Decimal(str(winner_count))
        fractional_remainder = total_distributable_funds - total_allocated_winnings

        with transaction.atomic():
            # Apply strict row-level locking concurrency safety patterns on single-row system accounts
            if fractional_remainder > 0:
                PlatformRevenueAccount.objects.get_or_create(
                    id=1, 
                    defaults={'account_name': 'Administrative Platform Revenue', 'balance': Decimal('0.00')}
                )
                admin_account = PlatformRevenueAccount.objects.select_for_update().get(id=1)
                admin_account.balance = models.F('balance') + fractional_remainder
                admin_account.save()
                logger.info(f"Swept KSh {fractional_remainder} fractional micro-penny remainder to administrative ledger record.")

            for session in winners:
                tx_receipt = session.transaction_reference

                if not tx_receipt:
                    logger.error(f"Could not find valid transaction receipt for Session {session.id}. Skipping payout entry.")
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

                # Explicitly binding parameters inside the lambda scope to protect loop variable execution mapping
                from quiz.tasks import send_victory_sms
                transaction.on_commit(
                    lambda tx=tx_receipt, s=session, p=raw_prize_share: send_victory_sms.delay(
                        tx.phone_number,
                        tx.player_name or "Valued Player",
                        s.score,
                        str(p)
                    )
                )

        logger.info(f"Pool ID {current_pool.id} settlement processing cycle executed cleanly and successfully closed.")