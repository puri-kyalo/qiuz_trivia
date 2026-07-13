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
    Normalizes Kenyan phone formats to a standard 254XXXXXXXX format.
    Handles 07..., 01..., 7..., 1..., and +254... variations.
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
        raise ValidationError("Phone number does not match Kenyan telecommunication boundaries.")
        
    return cleaned


class QuizUserProfile(models.Model):
    """
    Tracks long-term user assets, participation metadata, and onboarding attribution.
    """
    id = models.BigAutoField(primary_key=True)
    phone_number = models.CharField(max_length=20, unique=True, blank=True, null=True, db_index=True)
    wallet_balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    tickets_available = models.IntegerField(default=0)
    total_games_played = models.PositiveIntegerField(default=0)
    
    # Onboarding tracking
    onboarding_staff_name = models.CharField(max_length=150, blank=True, null=True)
    onboarding_staff_emp_number = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'quiz_user_profile'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(wallet_balance__gte=Decimal('0.00')),
                name='wallet_balance_cannot_be_negative'
            )
        ]

    def __str__(self):
        return f"{self.phone_number or 'Unassigned'} (Bal: KSh {self.wallet_balance})"

    def has_active_payment_for_pool(self):
        return self.tickets_available >= 1

    def save(self, *args, **kwargs):
        if self.phone_number:
            try:
                self.phone_number = normalize_kenyan_phone(self.phone_number)
            except ValidationError:
                pass
        super().save(*args, **kwargs)


class QuizPool(models.Model):
    """
    Manages periodic game cycles and prize allocations.
    """
    id = models.BigAutoField(primary_key=True)
    start_time = models.DateTimeField(db_index=True)
    end_time = models.DateTimeField(db_index=True)
    total_entry_fees_collected = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    grand_prize_pool = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    retained_company_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_entries = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
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
        return f"Pool {self.id} ({self.start_time.strftime('%Y-%m-%d %H:%M')} to {self.end_time.strftime('%Y-%m-%d %H:%M')})"

    def process_incoming_entry(self, entry_fee=Decimal('20.00')):
        half_allocation = entry_fee / Decimal('2.00')
        QuizPool.objects.filter(id=self.id).update(
            total_entry_fees_collected=F('total_entry_fees_collected') + entry_fee,
            grand_prize_pool=F('grand_prize_pool') + half_allocation,
            retained_company_earnings=F('retained_company_earnings') + half_allocation,
            total_entries=F('total_entries') + 1
        )


class QuestionBank(models.Model):
    """
    Question repository pre-allocated to specific quiz loops.
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
    Logs incoming transaction processing states from webhooks or manual status checks.
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

    paystack_reference = models.CharField(max_length=255, primary_key=True, db_index=True)
    user_profile = models.ForeignKey(QuizUserProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    player_name = models.CharField(max_length=150, blank=True, null=True)
    phone_number = models.CharField(max_length=20, db_index=True)
    email = models.EmailField(blank=True, null=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    
    access_token = models.CharField(max_length=64, unique=True, db_index=True)
    is_token_used = models.BooleanField(default=False, db_index=True)
    
    backend_metadata = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    payout_status = models.CharField(max_length=20, choices=PAYOUT_CHOICES, default='UNPROCESSED', db_index=True)

    class Meta:
        db_table = 'quiz_paystack_transaction'

    def __str__(self):
        return f"Ref: {self.paystack_reference} | Status: {self.status}"

    def save(self, *args, **kwargs):
        if self.phone_number:
            try:
                self.phone_number = normalize_kenyan_phone(self.phone_number)
            except ValidationError:
                pass
        super().save(*args, **kwargs)


class GameSession(models.Model):
    """
    Tracks runtime analytics and scores for validated tournament matches.
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
        constraints = [
            models.UniqueConstraint(
                fields=['transaction_reference', 'pool'],
                name='unique_game_per_transaction_pool'
            )
        ]

    def __str__(self):
        return f"Session {self.id} (Score: {self.score})"


class PayoutEvent(models.Model):
    """
    Audit log record tracking outgoing payouts issued to winners.
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending Processing'),
        ('SUCCESS', 'Payout Successfully Credited'),
        ('FAILED', 'Payout Execution Failed'),
    ]

    id = models.BigAutoField(primary_key=True)
    transaction_receipt = models.ForeignKey(PaystackTransaction, on_delete=models.PROTECT, related_name='payout_records')
    pool = models.ForeignKey(QuizPool, on_delete=models.PROTECT, related_name='payouts')
    game_session = models.OneToOneField(GameSession, on_delete=models.PROTECT, related_name='payout_event')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))])
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='PENDING')
    payout_reference = models.CharField(max_length=64, unique=True, editable=False, db_index=True)
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
        return f"{self.payout_reference} - KSh {self.amount}"


class PlatformRevenueAccount(models.Model):
    """
    Tracks system operational buffers and fractional micro-penny adjustments.
    """
    id = models.BigAutoField(primary_key=True)
    account_name = models.CharField(max_length=100, default="Administrative Platform Revenue")
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quiz_platform_revenue'

    def __str__(self):
        return f"{self.account_name} - KSh {self.balance}"