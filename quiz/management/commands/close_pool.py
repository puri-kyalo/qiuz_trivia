# quiz/management/commands/close_pool.py
from django.core.management.base import BaseCommand
from quiz.payouts import execute_automated_pool_payout

class Command(BaseCommand):
    help = "Closes the active trivia quiz pool, dynamically calculates prize pools, and distributes M-Pesa payouts."

    def handle(self, *args, **options):
        self.stdout.write("⏳ Initiating 3-hour dynamic pool evaluation sequence...")
        
        # Executes with NO hardcoded parameters—reads the pool database sums dynamically!
        execute_automated_pool_payout()
        
        self.stdout.write(self.style.SUCCESS("✅ Pool evaluated, winners handled, and new pool instantiated!"))