import logging
from decimal import Decimal
from celery import shared_task
from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=5, default_retry_delay=4)
def send_victory_sms(self, normalized_phone, player_name, score, prize_share_str):
    """
    Asynchronously transmits victory notifications to winning tournament players.
    Applies exponential backoff to recover cleanly from carrier network spikes.
    """
    # 1. International Prefix Injection Adapter (E.164 compliance model mapping)
    sanitized_phone = str(normalized_phone).strip()
    if sanitized_phone.startswith('254'):
        e164_phone = f"+{sanitized_phone}"
    elif sanitized_phone.startswith('0'):
        e164_phone = f"+254{sanitized_phone[1:]}"
    elif not sanitized_phone.startswith('+'):
        e164_phone = f"+{sanitized_phone}"
    else:
        e164_phone = sanitized_phone

    # Fixed copy context to accurately match your user verification layout
    message = (
        f"Congratulations {player_name}! You won KSh {prize_share_str} on PesaQuiz! "
        f"Score: {score}/10. Your reward balance ledger record has been processed instantly."
    )

    try:
        logger.info(f"Attempting production gateway transmission of victory SMS to target {e164_phone}...")
        
        # Live Africa's Talking Client Initialization
        import africastalking
        africastalking.initialize(
            username=settings.AFRICASTALKING_USERNAME, 
            api_key=settings.AFRICASTALKING_API_KEY
        )
        sms = africastalking.SMS
        
        # Synchronous API Network Execution Boundary Call
        response = sms.send(message, [e164_phone])
        
        # Parse and validate structural array responses
        recipients = response.get('SMSMessageData', {}).get('Recipients', [])
        if not recipients or recipients[0].get('status') not in ['Success', 'Sent']:
            status_reason = recipients[0].get('status') if recipients else "No recipient payload returned"
            raise RuntimeError(f"Carrier gateway transmission rejected payload context. Status: {status_reason}")
        
        logger.info(f"SMS successfully dispatched via Africa's Talking framework to {e164_phone}.")
        return f"SUCCESS_DISPATCH_TO_{e164_phone}"

    except Exception as exc:
        logger.warning(f"Carrier gateway processing anomaly caught: {exc}. Retrying...")
        # Hardened Exponential backoff calculation logic execution loop
        countdown_timer = self.default_retry_delay * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown_timer)


@shared_task
def seed_questions_for_pool(pool_id):
    """
    Finds template question banks and replicates them under the newly 
    spawned pool id reference mapping so active game sessions have traffic assets.
    """
    from quiz.models import QuizPool, QuestionBank
    try:
        pool = QuizPool.objects.get(id=pool_id)
        
        # Pulls up to 10 questions from historical pools to use as baseline seeding templates
        base_questions = QuestionBank.objects.filter(pool_id__lt=pool_id)[:10]
        
        if not base_questions.exists():
            logger.warning(f"No historical baseline question matrices found to seed Pool {pool_id}.")
            return False
            
        new_questions = []
        for q in base_questions:
            new_questions.append(QuestionBank(
                pool=pool,
                question_text=q.question_text,
                choice_1=q.choice_1,
                choice_2=q.choice_2,
                choice_3=q.choice_3,
                choice_4=q.choice_4,
                correct_choice=q.correct_choice,
                category=q.category
            ))
        
        QuestionBank.objects.bulk_create(new_questions)
        logger.info(f"Successfully bulk-seeded {len(new_questions)} question templates into new Pool ID: {pool_id}")
        return True
    except Exception as e:
        logger.error(f"Failed execution block during async question seeding sequence for pool {pool_id}: {str(e)}")
        return False


@shared_task
def run_management_command_rotation():
    """
    Periodic cron wrapper automation framework block invoked by Celery Beat.
    Triggers the high-precision tournament management command cycle every 2 hours.
    """
    logger.info("Celery Beat engine triggered periodic tournament lifecycle loop execution sequence.")
    call_command('rotate_quiz_pool')
    return "ROTATION_COMMAND_COMPLETED"