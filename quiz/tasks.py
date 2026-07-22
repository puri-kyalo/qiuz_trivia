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


@shared_task(bind=True, max_retries=3)
def run_management_command_rotation(self):
    """
    Periodic task triggered by Celery Beat every 3 hours.
    Rotates active quiz pools and seeds 200 questions via Gemini AI.
    """
    logger.info("Executing periodic 3-hour quiz pool rotation & AI seeding...")
    try:
        # 1. Rotate the active pool if you have a rotation command
        try:
            call_command('rotate_quiz_pool')
        except Exception as e:
            logger.warning(f"rotate_quiz_pool command skipped or failed: {e}")

        # 2. Seed 200 dynamic questions into the active pool
        call_command('seed_quiz_questions', count=200)

        logger.info("Successfully executed 3-hour pool rotation and seeded 200 questions.")
        return "ROTATION_AND_SEEDING_COMPLETE"

    except Exception as exc:
        logger.error(f"Error during pool rotation/seeding task: {str(exc)}")
        # Retry in 5 minutes if something fails (e.g., temporary network glitch)
        raise self.retry(exc=exc, countdown=300)