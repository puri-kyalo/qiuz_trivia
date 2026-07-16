import logging
from django.core.management.base import BaseCommand
from quiz.payouts import execute_automated_pool_payout

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Rotates quiz pools and processes payouts through Paystack."

    def handle(self, *args, **options):
        self.stdout.write("Initiating automated pool rotation and payout sequence...")
        
        try:
            # We delegate all the heavy lifting to our single source of truth in payouts.py
            execute_automated_pool_payout()
            self.stdout.write(self.style.SUCCESS("Successfully rotated pools and processed winners."))
        except Exception as e:
            logger.error(f"Error during pool rotation command execution: {str(e)}")
            self.stdout.write(self.style.ERROR(f"Rotation failed: {str(e)}"))