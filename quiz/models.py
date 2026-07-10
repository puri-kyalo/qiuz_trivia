# quiz/models.py
import re
import uuid
from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db.models import F
from django.utils import timezone

def normalize_kenyan_phone(phone_str):
    """
    Normalizes variations of Kenyan phone formats into a unified 254XXXXXXXX structure.
    Supports prefixes: 07..., 01..., 7..., 1..., +254...
    """
    if not phone_str:
        return None
        
    cleaned = re.sub(r'\D', '', str(phone_str))
    
    if cleaned.startswith('254') and len(cleaned) == 12:
        pass
    elif cleaned.startswith('0') and len(cleaned) == 10:
        cleaned = '254' + cleaned[1:]
    elif (cleaned.startswith('7') or cleaned.startswith('1')) and len(cleaned) == 9:
        cleaned = '254' + cleaned
    else:
        raise ValidationError("Invalid Kenyan phone number format layout.")
        
    if not re.match(r'^254(7|1)\d{8}$', cleaned):
        raise ValidationError("Phone number does not match E.164 compliance boundaries for Kenya.")
        
    return cleaned


class QuizPool(models.Model):
    """
    Represents a rigid 3-hour tournament block hosting active traffic tiers.
    Tracks entry fees collected and manages a strict 50/50 balance allocation ledger split.
    """
    id = models.BigAutoField(primary_key=True)
    start_time = models.DateTimeField(db_index=True)
    end_time = models.DateTimeField(db_index=True)
    total_entry_fees_collected = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    grand_prize_pool = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    retained_company_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_entries = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    
    # Phase 5 Isolation Audit Flag
    requires_manual_audit = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = 'quiz_pool'
        indexes = [
            models.Index(fields=['is_active', 'start_time', 'end_time'], name='quiz_pool_perf_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(grand_prize_pool__gte=Decimal('0.00')),
                name='grand_prize_pool_cannot_be_negative'
            )
        ]

    def __str__(self):
        return f"Pool {self.id} ({self.start_time} to {self.end_time})"

    def process_incoming_entry(self, entry_fee=Decimal('20.00')):
        """
        Executes an atomic 50/50 corporate/pool ledger split down at the database layer
        to avoid race conditions during massive entry concurrency.
        """
        half_allocation = entry_fee / Decimal('2.00')
        QuizPool.objects.filter(id=self.id).update(
            total_entry_fees_collected=F('total_entry_fees_collected') + entry_fee,
            grand_prize_pool=F('grand_prize_pool') + half_allocation,
            retained_company_earnings=F('retained_company_earnings') + half_allocation,
            total_entries=F('total_entries') + 1
        )


class QuestionBank(models.Model):
    """
    Pre-seeded question banks categorized cleanly for balanced moderate-difficulty loads.
    """
    CATEGORY_CHOICES = [
        ('LOCAL_FOOTBALL', 'Local Kenyan Football'),
        ('KENYAN_HISTORY', 'Kenyan History'),
        ('WORLD_FOOTBALL', 'World General Football'),
    ]
    
    id = models.BigAutoField(primary_key=True)
    pool = models.ForeignKey(QuizPool, on_delete=models.CASCADE, related_name='questions', db_index=True)
    question_text = models.TextField()
    choice_1 = models.CharField(max_length=255)
    choice_2 = models.CharField(max_length=255)
    choice_3 = models.CharField(max_length=255)
    choice_4 = models.CharField(max_length=255)
    correct_choice = models.PositiveSmallIntegerField()  
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, db_index=True)

    class Meta:
        db_table = 'quiz_question_bank'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(correct_choice__gte=1, correct_choice__lte=4),
                name='valid_choice_index_range'
            )
        ]

    def __str__(self):
        return f"[{self.category}] {self.question_text[:40]}..."


class PaystackTransaction(models.Model):
    """
    Acts as the single-use security barrier and player identity tracker.
    Stores real M-Pesa registration details from the payment payload.
    Tracks automated M-Pesa compensation processing loops.
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    ]

    PAYOUT_CHOICES = [
        ('UNPROCESSED', 'Active Pool / Unprocessed'),
        ('PENDING', 'Pending Payout'),
        ('PAID', 'Paid Successfully'),
        ('FAILED', 'Payout Failed'),
        ('EVALUATED', 'Closed Pool / No Prize'),
    ]

    # 💳 Existing Core Payment Fields
    paystack_reference = models.CharField(max_length=255, primary_key=True, db_index=True)
    player_name = models.CharField(max_length=150, blank=True, null=True, help_text="Captured M-Pesa Name")
    phone_number = models.CharField(max_length=20, db_index=True)
    email = models.EmailField(blank=True, null=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    
    access_token = models.CharField(max_length=64, unique=True, db_index=True)
    is_token_used = models.BooleanField(default=False, db_index=True)
    
    backend_metadata = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    # 🏆 Automated Compensation Management state 
    payout_status = models.CharField(
        max_length=20, 
        choices=PAYOUT_CHOICES, 
        default='UNPROCESSED', 
        db_index=True,
        help_text="Tracks clean handoffs across recurring 3-hour pools"
    )

    class Meta:
        db_table = 'quiz_paystack_transaction'

    def __str__(self):
        return f"Ref: {self.paystack_reference} | Player: {self.player_name} ({self.status})"

    def save(self, *args, **kwargs):
        """Auto-normalizes Kenyan numbers before database write"""
        if self.phone_number:
            try:
                self.phone_number = normalize_kenyan_phone(self.phone_number)
            except (ValidationError, NameError):
                pass
        super().save(*args, **kwargs)


class GameSession(models.Model):
    """
    Maintains wall-clock verification tracking mapped back to a specific payment receipt.
    Bypasses standard Django User schemas entirely.
    """
    id = models.BigAutoField(primary_key=True)
    transaction_reference = models.OneToOneField(
        PaystackTransaction, 
        on_delete=models.CASCADE, 
        related_name='game_session',
        db_column='transaction_reference'
    )
    pool = models.ForeignKey(QuizPool, on_delete=models.PROTECT, related_name='game_sessions')
    start_time = models.DateTimeField(db_index=True)
    end_time = models.DateTimeField(db_index=True, blank=True, null=True)
    duration_seconds = models.FloatField(db_index=True, blank=True, null=True)
    score = models.PositiveSmallIntegerField(blank=True, null=True)
    is_disqualified = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = 'quiz_game_session'

    def __str__(self):
        return f"Session {self.id} (Score: {self.score} in {self.duration_seconds}s)"


# ==========================================
# PHASE 5 ADDITIONS: HIGH-INTEGRITY LEDGERS
# ==========================================

class PayoutEvent(models.Model):
    """
    Maintains a strict, un-deletable record of all outgoing real-money rewards 
    distributed to quiz winners to protect the platform balance sheets.
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending Processing'),
        ('SUCCESS', 'Payout Successfully Credited'),
        ('FAILED', 'Payout Execution Failed'),
    ]

    id = models.BigAutoField(primary_key=True)
    transaction_receipt = models.ForeignKey(
        PaystackTransaction, 
        on_delete=models.PROTECT, 
        related_name='payout_records'
    )
    pool = models.ForeignKey(
        QuizPool, 
        on_delete=models.PROTECT, 
        related_name='payouts'
    )
    game_session = models.OneToOneField(
        GameSession, 
        on_delete=models.PROTECT, 
        related_name='payout_event'
    )
    amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    status = models.CharField(
        max_length=15, 
        choices=STATUS_CHOICES, 
        default='PENDING'
    )
    payout_reference = models.CharField(
        max_length=64, 
        unique=True, 
        editable=False,
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'quiz_payout_event'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at'], name='payout_status_date_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self.payout_reference:
            self.payout_reference = f"PAY-{uuid.uuid4().hex.upper()[:16]}"
        super().save(*args, **kwargs)

    def __str__(self):
        player_ident = self.transaction_receipt.player_name or "Unknown"
        return f"{self.payout_reference} - {player_ident} - KSh {self.amount}"


class PlatformRevenueAccount(models.Model):
    """
    A single-row tracking model used to hold platform financial accounts 
    and absorb fractional micro-penny remainders from rounding ties.
    """
    id = models.BigAutoField(primary_key=True)
    account_name = models.CharField(max_length=100, default="Administrative Platform Revenue")
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quiz_platform_revenue'

    def __str__(self):
        return f"{self.account_name} - Bal: KSh {self.balance}"