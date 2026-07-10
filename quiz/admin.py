from django.contrib import admin
from .models import QuizPool, QuestionBank, PaystackTransaction, GameSession, PayoutEvent, PlatformRevenueAccount

@admin.register(QuizPool)
class QuizPoolAdmin(admin.ModelAdmin):
    list_display = ('id', 'start_time', 'end_time', 'total_entries', 'grand_prize_pool', 'retained_company_earnings', 'is_active', 'requires_manual_audit')
    list_filter = ('is_active', 'requires_manual_audit', 'start_time')
    search_fields = ('id',)
    ordering = ('-id',)
    readonly_fields = ('total_entry_fees_collected', 'grand_prize_pool', 'retained_company_earnings', 'total_entries')

@admin.register(QuestionBank)
class QuestionBankAdmin(admin.ModelAdmin):
    list_display = ('id', 'pool', 'category', 'question_text_short', 'correct_choice')
    list_filter = ('category', 'pool')
    search_fields = ('question_text',)
    
    def question_text_short(self, obj):
        return obj.question_text[:60] + "..." if len(obj.question_text) > 60 else obj.question_text
    question_text_short.short_description = "Question Text"

@admin.register(PaystackTransaction)
class PaystackTransactionAdmin(admin.ModelAdmin):
    list_display = ('paystack_reference', 'player_name', 'phone_number', 'amount', 'status', 'is_token_used', 'created_at')
    list_filter = ('status', 'is_token_used', 'created_at')
    search_fields = ('paystack_reference', 'player_name', 'phone_number', 'email')
    ordering = ('-created_at',)
    readonly_fields = ('paystack_reference', 'access_token', 'created_at')

@admin.register(GameSession)
class GameSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'get_player_phone', 'pool', 'score', 'duration_seconds', 'is_disqualified', 'start_time')
    list_filter = ('is_disqualified', 'pool', 'start_time')
    search_fields = ('id', 'transaction_reference__phone_number', 'transaction_reference__player_name')
    ordering = ('-id',)
    list_select_related = ('transaction_reference', 'pool') # Prevents N+1 query database spikes

    def get_player_phone(self, obj):
        return obj.transaction_reference.phone_number
    get_player_phone.short_description = "Player Phone"

@admin.register(PayoutEvent)
class PayoutEventAdmin(admin.ModelAdmin):
    list_display = ('payout_reference', 'get_player_name', 'get_player_phone', 'pool', 'amount', 'status', 'created_at')
    list_filter = ('status', 'created_at', 'pool')
    search_fields = ('payout_reference', 'transaction_receipt__phone_number', 'transaction_receipt__player_name')
    ordering = ('-created_at',)
    list_select_related = ('transaction_receipt', 'pool', 'game_session')

    def get_player_name(self, obj):
        return obj.transaction_receipt.player_name or "N/A"
    get_player_name.short_description = "Winner Name"

    def get_player_phone(self, obj):
        return obj.transaction_receipt.phone_number
    get_player_phone.short_description = "Winner Phone"

@admin.register(PlatformRevenueAccount)
class PlatformRevenueAccountAdmin(admin.ModelAdmin):
    list_display = ('account_name', 'balance', 'updated_at')
    readonly_fields = ('updated_at',)
