# quiz/views.py
import json
import logging
import random
import uuid
import time
from decimal import Decimal
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import transaction
from django.utils import timezone
from django.db.models import F

import requests
from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


from .models import QuizPool, PaystackTransaction, QuestionBank, GameSession
from .paystack_utils import verify_paystack_signature, verify_paystack_transaction

logger = logging.getLogger(__name__)

GLOBAL_SESSION_LIMIT_SECONDS = 80.0
LATENCY_GRACE_WINDOW_SECONDS = 5.0



def initiate_payment(request):
    if request.method == "POST":
        player_name = request.POST.get('player_name')
        phone = request.POST.get('phone_number')
        email = request.POST.get('email', f"{phone}@trivia.com")  # Paystack requires an email format
        amount = 20.00  # Entry fee in KSh
        
        # FIX STEP 1: Generate an authentic 36-character hex UUID string
        # This will satisfy the database UUIDField and act as our tracking key
        payment_uuid = str(uuid.uuid4())
        
        # 1. Initialize data with Paystack API
        url = "https://api.paystack.co/transaction/initialize"
        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        }
        
        callback_url = request.build_absolute_uri(reverse('quiz:paystack_callback'))
        
        payload = {
            "email": email,
            "amount": int(amount * 100),  # Paystack counts in cents
            "callback_url": callback_url,
            # FIX STEP 2: Force Paystack to use our clean UUID string as its transaction reference key
            "reference": payment_uuid, 
            "metadata": {
                "player_name": player_name,
                "phone_number": phone
            }
        }
        
        try:
            response = requests.post(url, json=payload, headers=headers).json()
        except requests.exceptions.RequestException:
            # Fallback handling in case Paystack API goes temporarily unreachable
            return render(request, 'quiz/landing.html', {'error': 'Payment gateway unreachable.'})
        
        if response.get('status'):
            # 2. Save the transaction baseline tracking model in pending status
            PaystackTransaction.objects.create(
                # 🔥 FIXED CRITICAL FIELD: Satisfies the UNIQUE constraint requirement on the database table
                paystack_reference=payment_uuid, 
                
                access_token=payment_uuid, 
                player_name=player_name,
                phone_number=phone,
                email=email,
                amount=amount,
                status='PENDING'
            )
            # 3. Securely redirect user straight out to Paystack's hosted payment gateway interface
            return redirect(response['data']['authorization_url'])
            
    return render(request, 'quiz/landing.html')

def paystack_callback(request):
    reference = request.GET.get('reference')  # This will now safely return our UUID string back from Paystack
    
    if not reference:
        return redirect('quiz:payment_failed')

    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
    }
    
    try:
        response = requests.get(url, headers=headers).json()
    except Exception:
        return redirect('quiz:payment_failed')

    if response.get('status') and response['data']['status'] == 'success':
        # Safely looks up the transaction using the returned UUID string
        tx = PaystackTransaction.objects.filter(access_token=reference).first()
        
        if tx:
            tx.status = 'SUCCESS'
            metadata = response['data'].get('metadata', {})
            tx.player_name = metadata.get('player_name', tx.player_name)
            tx.phone_number = metadata.get('phone_number', tx.phone_number)
            tx.save()
            
            base_url = reverse('quiz:quiz_play')
            return redirect(f"{base_url}?token={tx.access_token}")
            
    return redirect('quiz:payment_failed')

# ==============================================================================
# MAIN PAGE RENDERING & RESULTS LAYER
# ==============================================================================

def quiz_play_view(request):
    """
    Renders the live quiz layout template and provides initial server state context.
    """
    current_now = timezone.now()
    active_pool = QuizPool.objects.filter(
        is_active=True,
        start_time__lte=current_now,
        end_time__gte=current_now
    ).first()

    pool_id = active_pool.id if active_pool else 1
    target_timestamp_ms = int((time.time() + GLOBAL_SESSION_LIMIT_SECONDS) * 1000)

    context = {
        'session_id': pool_id,
        'target_timestamp_ms': target_timestamp_ms,
        'duration_limit_seconds': int(GLOBAL_SESSION_LIMIT_SECONDS),
        'payment_status': 'PENDING',
        'transaction_reference': request.GET.get('reference', 'MPESA_REF_PENDING'),
        'access_token': request.GET.get('token', '')
    }
    return render(request, 'play.html', context)


def quiz_results_view(request):
    """
    Aggregates metrics for display on performance feedback screens post-submission.
    """
    session_id = request.GET.get('session_id')
    if not session_id:
        return HttpResponse("Missing tracking context session attribute parameter.", status=400)

    session = GameSession.objects.filter(id=session_id).first()
    if not session:
        return HttpResponse("Target ledger record could not be resolved.", status=404)

    context = {
        'score': session.score,
        'duration': round(session.duration_seconds, 2),
        'player_name': session.transaction_reference.player_name,
        'is_disqualified': session.is_disqualified,
        'pool_id': session.pool.id
    }
    return render(request, '/quiz/', context)


# ==============================================================================
# PHASE 2: PAYSTACK WEBHOOK & GUEST SAFETY NET VIEWS
# ==============================================================================

@csrf_exempt
@require_POST
def paystack_webhook(request):
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
        tx.save()

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
    try:
        body = json.loads(request.body.decode('utf-8'))
        reference = body.get('reference')
    except (ValueError, KeyError, AttributeError):
        return JsonResponse({'success': False, 'status': 'FAILED', 'error': 'Malformed request body payload format.'}, status=400)

    if not reference:
        return JsonResponse({'success': False, 'status': 'FAILED', 'error': 'Reference query parameter required.'}, status=400)

    verification_result = verify_paystack_transaction(reference)
    if not verification_result or verification_result.get('status') != 'SUCCESS':
        return JsonResponse({'success': False, 'status': 'FAILED', 'error': 'Payment verification failed at payment gateway.'}, status=422)

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
            return JsonResponse({'success': True, 'status': 'SUCCESS', 'detail': 'Processed via webhook loop.', 'access_token': str(tx.access_token)})

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

    return JsonResponse({'success': True, 'status': 'SUCCESS', 'detail': 'Safety net verification complete.', 'access_token': str(tx.access_token)})


# ==============================================================================
# PHASE 3: SECURE TOKEN GAMEPLAY LOOP VIEWS
# ==============================================================================

@require_POST
def start_quiz_session(request):
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
        'choices': [q.choice_1, q.choice_2, q.choice_3, q.choice_4],
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
            session.score = 0
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