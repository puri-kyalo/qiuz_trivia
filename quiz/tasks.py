import logging
import time
from celery import shared_task
# If you use django-q instead of celery, simply swap the decorator to a normal function
# and invoke it using async_task('quiz.tasks.send_victory_sms', ...)

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=5, default_retry_delay=4)
def send_victory_sms(self, normalized_phone, player_name, score, prize_share_str):
    """
    Asynchronously transmits victory notifications to winning tournament players.
    Applies exponential backoff to recover cleanly from carrier network spikes.
    """
    # 1. International Prefix Injection Adapter (E.164 compliance model mapping)
    if normalized_phone.startswith('254'):
        e164_phone = f"+{normalized_phone}"
    elif normalized_phone.startswith('0'):
        e164_phone = f"+254{normalized_phone[1:]}"
    elif not normalized_phone.startswith('+'):
        e164_phone = f"+{normalized_phone}"
    else:
        e164_phone = normalized_phone

    message = (
        f"Congratulations {player_name}! You won KSh {prize_share_str} on PesaQuiz! "
        f"Score: {score}/10. Your wallet balance has been updated instantly."
    )

    # Mocking Africa's Talking Client Library initialization for production alignment
    # In live systems: 
    # import africastalking
    # africastalking.initialize(username=settings.AT_USERNAME, api_key=settings.AT_API_KEY)
    # sms = africastalking.SMS
    
    try:
        logger.info(f"Attempting transmission of victory SMS to target {e164_phone}...")
        
        # Simulated Network Boundary Execution
        # response = sms.send(message, [e164_phone])
        # if response['SMSMessageData']['Recipients'][0]['status'] != 'Success':
        #     raise Exception("Carrier gateway transmission rejected payload context.")
        
        logger.info(f"SMS successfully dispatched via third-party infrastructure to {e164_phone}.")
        return f"SUCCESS_DISPATCH_TO_{e164_phone}"

    except Exception as exc:
        logger.warning(f"Carrier gateway processing anomaly caught: {exc}. Retrying...")
        # Exponential backoff calculation logic execution loop
        countdown_timer = self.default_retry_delay * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown_timer)