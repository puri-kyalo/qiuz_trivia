# quiz/views.py
import json
import logging
import random
import uuid
from decimal import Decimal
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import transaction
from django.utils import timezone
from django.db.models import F

from .models import QuizPool, PaystackTransaction, QuestionBank, GameSession
from .paystack_utils import verify_paystack_signature, verify_paystack_transaction

logger = logging.getLogger(__name__)

GLOBAL_SESSION_LIMIT_SECONDS = 80.0
LATENCY_GRACE_WINDOW_SECONDS = 5.0

# ==============================================================================
# PHASE 2: PAYSTACK WEBHOOK & GUEST SAFETY NET VIEWS
# ==============================================================================

@csrf_exempt
@require_POST
def paystack_webhook(request):
    """
    High-Throughput Webhook Listener capturing asynchronous charge.success updates.
    Fulfills 50/50 balance allocations and leaves string formatting tasks safely to model saves.
    """
    signature = request.META.get('HTTP_X_PAYSTACK_SIGNATURE')
    if not signature or not verify_paystack_signature(request.body, signature):
        return HttpResponse("Unauthorized Signature Header Missing or Spoofed.", status=401)

    try:
        event_data = json.loads(request.body.decode('utf-8'))
    except ValueError:
        return HttpResponse("Invalid JSON Payload Structure.", status=400)

    if event_data.get('event') != 'charge.success':
        return JsonResponse({'status': 'ignored_event'})

    data = event_data.get('data', {})
    reference = data.get('reference')
    
    if not reference:
        return HttpResponse("Missing Reference Token Parameter.", status=400)

    amount_minor = Decimal(str(data.get('amount', 0)))
    amount_kes = amount_minor / Decimal('100.00')
    customer_email = data.get('customer', {}).get('email')
    
    authorization = data.get('authorization', {})
    gateway_sender_name = authorization.get('sender_name') or data.get('metadata', {}).get('player_name')
    raw_phone = data.get('customer', {}).get('phone') or data.get('metadata', {}).get('phone')
    
    player_name = gateway_sender_name if gateway_sender_name else "M-Pesa Guest Subscriber"
    fallback_phone = str(raw_phone) if raw_phone else "00000000000"

    with transaction.atomic():
        tx, created = PaystackTransaction.objects.select_for_update().get_or_create(
            paystack_reference=reference,
            defaults={
                'player_name': player_name,
                'phone_number': fallback_phone,
                'email': customer_email,
                'amount': amount_kes,
                'status': 'PENDING',
                'access_token': uuid.uuid4(),
                'is_token_used': False
            }
        )

        if tx.status == 'SUCCESS':
            return JsonResponse({'status': 'duplicate_prevented', 'access_token': str(tx.access_token)})

        tx.status = 'SUCCESS'
        tx.player_name = player_name
        tx.phone_number = fallback_phone
        if not tx.access_token:
            tx.access_token = uuid.uuid4()
        tx.save()  # Explicit save executes the model's clean number normalization

        # Process 50/50 Prize Pool Distribution Split Atomically
        current_now = timezone.now()
        active_pool = QuizPool.objects.filter(
            is_active=True, 
            start_time__lte=current_now, 
            end_time__gte=current_now
        ).first()

        if active_pool:
            active_pool.process_incoming_entry(entry_fee=amount_kes)

    return JsonResponse({'status': 'fulfilled_successfully', 'access_token': str(tx.access_token)})


@require_POST
def verify_user_payment(request):
    """
    The Guest Transaction Safety Net view addressing client drops manually.
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
        reference = body.get('reference')
    except (ValueError, KeyError, AttributeError):
        return JsonResponse({'success': False, 'error': 'Malformed request body payload format.'}, status=400)

    if not reference:
        return JsonResponse({'success': False, 'error': 'Reference query parameter required.'}, status=400)

    verification_result = verify_paystack_transaction(reference)
    if not verification_result or verification_result.get('status') != 'SUCCESS':
        return JsonResponse({'success': False, 'error': 'Payment verification failed at payment gateway.'}, status=422)

    amount_kes = verification_result.get('amount', Decimal('20.00'))
    customer_email = verification_result.get('email')
    
    gateway_sender_name = verification_result.get('sender_name') or verification_result.get('metadata', {}).get('player_name')
    raw_phone = verification_result.get('phone') or verification_result.get('metadata', {}).get('phone')
    
    player_name = gateway_sender_name if gateway_sender_name else "M-Pesa Guest Subscriber"
    fallback_phone = str(raw_phone) if raw_phone else "00000000000"

    with transaction.atomic():
        tx, created = PaystackTransaction.objects.select_for_update().get_or_create(
            paystack_reference=reference,
            defaults={
                'player_name': player_name,
                'phone_number': fallback_phone,
                'email': customer_email,
                'amount': amount_kes,
                'status': 'PENDING',
                'access_token': uuid.uuid4(),
                'is_token_used': False
            }
        )

        if tx.status == 'SUCCESS':
            return JsonResponse({'success': True, 'detail': 'Processed via webhook loop.', 'access_token': str(tx.access_token)})

        tx.status = 'SUCCESS'
        tx.player_name = player_name
        tx.phone_number = fallback_phone
        if not tx.access_token:
            tx.access_token = uuid.uuid4()
        tx.save()

        current_now = timezone.now()
        active_pool = QuizPool.objects.filter(
            is_active=True, 
            start_time__lte=current_now, 
            end_time__gte=current_now
        ).first()

        if active_pool:
            active_pool.process_incoming_entry(entry_fee=amount_kes)

    return JsonResponse({'success': True, 'detail': 'Safety net verification complete.', 'access_token': str(tx.access_token)})


# ==============================================================================
# PHASE 3: SECURE TOKEN GAMEPLAY LOOP VIEWS
# ==============================================================================

@require_POST
def start_quiz_session(request):
    """
    Validates dynamic tokens, grabs 10 randomized unique question IDs inside Python memory,
    and returns questions with answers stripped out to protect against client injection exploits.
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
        token_str = body.get('access_token')
    except (ValueError, AttributeError):
        return JsonResponse({'success': False, 'error': 'Malformed Payload JSON Structure.'}, status=400)

    if not token_str:
        return JsonResponse({'success': False, 'error': 'Secure authentication access token required.'}, status=401)

    current_now = timezone.now()
    current_pool = QuizPool.objects.filter(
        is_active=True,
        start_time__lte=current_now,
        end_time__gte=current_now
    ).first()

    if not current_pool:
        return JsonResponse({'success': False, 'error': 'No active tournament pool window available.'}, status=400)

    with transaction.atomic():
        tx = PaystackTransaction.objects.select_for_update().filter(access_token=token_str, status='SUCCESS').first()
        
        if not tx:
            return JsonResponse({'success': False, 'error': 'Access Token invalid, unpaid, or mismatched.'}, status=401)
        if tx.is_token_used:
            return JsonResponse({'success': False, 'error': 'This payment pass has already been used.'}, status=403)

        tx.is_token_used = True
        tx.save()

        all_question_ids = list(QuestionBank.objects.filter(pool=current_pool).values_list('id', flat=True))
        
        if len(all_question_ids) < 10:
            return JsonResponse({'success': False, 'error': 'Insufficient pool allocations populated.'}, status=503)

        selected_ids = random.sample(all_question_ids, 10)
        questions = list(QuestionBank.objects.filter(id__in=selected_ids).only(
            'id', 'question_text', 'choice_1', 'choice_2', 'choice_3', 'choice_4', 'category'
        ))
        
        random.shuffle(questions)

        session = GameSession.objects.create(
            transaction_reference=tx,
            pool=current_pool,
            start_time=timezone.now(),
            duration_seconds=0.0
        )

    payload_questions = [{
        'id': q.id,
        'question_text': q.question_text,
        'choices': {
            '1': q.choice_1,
            '2': q.choice_2,
            '3': q.choice_3,
            '4': q.choice_4
        },
        'category': q.category
    } for q in questions]

    return JsonResponse({
        'success': True,
        'session_id': session.id,
        'global_limit_seconds': GLOBAL_SESSION_LIMIT_SECONDS,
        'questions': payload_questions,
        'player_name': tx.player_name
    })


@require_POST
def submit_quiz_answers(request):
    """
    Accepts quiz entries, tracks precision speed calculations, and scores answers securely.
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
        session_id = body.get('session_id')
        client_answers = body.get('answers', {})
    except (ValueError, AttributeError):
        return JsonResponse({'success': False, 'error': 'Malformed submission body format.'}, status=400)

    if not session_id:
        return JsonResponse({'success': False, 'error': 'Session identifier is required.'}, status=400)

    server_end_time = timezone.now()

    with transaction.atomic():
        session = GameSession.objects.select_for_update().filter(id=session_id).first()

        if not session:
            return JsonResponse({'success': False, 'error': 'Target game session could not be resolved.'}, status=404)

        if session.end_time is not None:
            return JsonResponse({'success': False, 'error': 'Submission rejected. Game session settled previously.'}, status=403)

        session.end_time = server_end_time
        elapsed_delta = session.end_time - session.start_time
        total_seconds = float(elapsed_delta.total_seconds())
        session.duration_seconds = total_seconds

        max_allowed_time = GLOBAL_SESSION_LIMIT_SECONDS + LATENCY_GRACE_WINDOW_SECONDS
        if total_seconds > max_allowed_time:
            session.is_disqualified = True
            session.save()
            return JsonResponse({
                'success': False, 
                'error': 'Session disqualified. Submission exceeded allowed duration metrics.',
                'duration_seconds': total_seconds
            }, status=408)

        question_ids = [int(qid) for qid in client_answers.keys() if qid.isdigit()]
        database_questions = QuestionBank.objects.filter(id__in=question_ids, pool=session.pool)

        calculated_score = 0
        for question in database_questions:
            submitted_choice = client_answers.get(str(question.id))
            if submitted_choice is not None and int(submitted_choice) == question.correct_choice:
                calculated_score += 1

        session.score = calculated_score
        session.save()

    return JsonResponse({
        'success': True,
        'score': calculated_score,
        'duration_seconds': total_seconds,
        'player_name': session.transaction_reference.player_name
    })