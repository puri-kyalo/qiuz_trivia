import logging
from django.core.management.base import BaseCommand
from quiz.payouts import execute_automated_pool_payout

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Evaluates active pool earnings, manages payouts, and opens the next pool slot."

    def handle(self, *args, **options):
        self.stdout.write("Starting pool finalization sequence...")
        try:
            execute_automated_pool_payout()
            self.stdout.write(self.style.SUCCESS("Successfully closed pool and processed pending balances."))
        except Exception as e:
            logger.error(f"Pool transition failed: {str(e)}")
            self.stdout.write(self.style.ERROR(f"Execution failed: {str(e)}"))