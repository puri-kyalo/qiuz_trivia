import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from quiz.models import QuizPool
from quiz.payouts import execute_automated_pool_payout

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Rotates quiz pools, creates a new 3-hour pool, and processes payouts through Paystack."

    def handle(self, *args, **options):
        self.stdout.write("Initiating automated pool rotation and payout sequence...")

        try:
            # 1. Process payouts for the outgoing pool
            execute_automated_pool_payout()
            self.stdout.write(self.style.SUCCESS("Payouts processed successfully."))

            # 2. Deactivate all existing active pools to prevent active pool collisions
            deactivated_count = QuizPool.objects.filter(is_active=True).update(is_active=False)
            self.stdout.write(f"Deactivated {deactivated_count} active pool(s).")

            # 3. Create the new 3-hour active pool
            now = timezone.now()
            new_pool = QuizPool.objects.create(
                is_active=True,
                start_time=now,
                end_time=now + timedelta(hours=3),
                grand_prize_pool=0.00,
                total_entries=0
            )

            self.stdout.write(
                self.style.SUCCESS(f"Successfully created and activated new Pool #{new_pool.id}.")
            )
            logger.info(f"Automated rotation complete. Pool #{new_pool.id} is now active.")

        except Exception as e:
            logger.error(f"Error during pool rotation command execution: {str(e)}")
            self.stdout.write(self.style.ERROR(f"Rotation failed: {str(e)}"))