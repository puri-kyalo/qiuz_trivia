import logging
from celery import shared_task
from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)

@shared_task(
    bind=True, 
    max_retries=5, 
    default_retry_delay=4, 
    exponential_backoff=True
)
def send_victory_sms(self, normalized_phone, player_name, score, prize_share_str):
    """
    Sends an SMS victory notification to tournament winners via Africa's Talking.
    """
    # Clean phone number and convert to E.164 format
    sanitized_phone = str(normalized_phone).strip()
    if sanitized_phone.startswith('254'):
        e164_phone = f"+{sanitized_phone}"
    elif sanitized_phone.startswith('0'):
        e164_phone = f"+254{sanitized_phone[1:]}"
    elif not sanitized_phone.startswith('+'):
        e164_phone = f"+{sanitized_phone}"
    else:
        e164_phone = sanitized_phone

    message = (
        f"Congratulations {player_name}! You won KSh {prize_share_str} on PesaQuiz! "
        f"Score: {score}/10. Your reward has been processed successfully."
    )

    try:
        logger.info(f"Sending victory SMS to {e164_phone}...")
        
        import africastalking
        africastalking.initialize(
            username=settings.AFRICASTALKING_USERNAME, 
            api_key=settings.AFRICASTALKING_API_KEY
        )
        sms = africastalking.SMS
        
        response = sms.send(message, [e164_phone])
        
        recipients = response.get('SMSMessageData', {}).get('Recipients', [])
        if not recipients or recipients[0].get('status') not in ['Success', 'Sent']:
            status_reason = recipients[0].get('status') if recipients else "No recipient data returned"
            raise RuntimeError(f"SMS delivery rejected by carrier status: {status_reason}")
        
        logger.info(f"SMS successfully delivered to {e164_phone}.")
        return f"SUCCESS_{e164_phone}"

    except Exception as exc:
        logger.warning(f"SMS delivery failed to {e164_phone}: {exc}. Retrying...")
        raise self.retry(exc=exc)


@shared_task
def seed_questions_for_pool(pool_id):
    """
    Copies a baseline set of historical questions to populate a newly created quiz pool.
    """
    from quiz.models import QuizPool, QuestionBank
    try:
        pool = QuizPool.objects.get(id=pool_id)
        
        # Pull 10 previous questions to use as a baseline pool template
        base_questions = QuestionBank.objects.filter(pool_id__lt=pool_id)[:10]
        
        if not base_questions.exists():
            logger.warning(f"No historical questions found to seed Pool ID: {pool_id}")
            return False
            
        new_questions = [
            QuestionBank(
                pool=pool,
                question_text=q.question_text,
                choice_1=q.choice_1,
                choice_2=q.choice_2,
                choice_3=q.choice_3,
                choice_4=q.choice_4,
                correct_choice=q.correct_choice,
                category=q.category
            )
            for q in base_questions
        ]
        
        QuestionBank.objects.bulk_create(new_questions)
        logger.info(f"Successfully cloned {len(new_questions)} baseline questions into Pool ID: {pool_id}")
        return True
    except Exception as e:
        logger.error(f"Error seeding questions for pool {pool_id}: {str(e)}")
        return False


@shared_task
def run_management_command_rotation():
    """
    Periodic task triggered by Celery Beat to rotate active quiz pools.
    """
    logger.info("Executing periodic quiz pool rotation command.")
    call_command('rotate_quiz_pool')
    return "ROTATION_COMPLETE"