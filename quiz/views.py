import hmac
import hashlib
import json
import logging
import random
import uuid
from decimal import Decimal
import requests

from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_POST
from django.db import transaction
from django.utils import timezone
from django.urls import reverse
from django.conf import settings
from django.db.models import Q
from django_ratelimit.decorators import ratelimit

from .models import QuizPool, PaystackTransaction, QuestionBank, GameSession
from .paystack_utils import verify_paystack_transaction

logger = logging.getLogger(__name__)

GLOBAL_SESSION_LIMIT_SECONDS = 80.0
LATENCY_GRACE_WINDOW_SECONDS = 5.0


def landing_page(request):
    return render(request, 'quiz/play.html')


@csrf_protect
@ratelimit(key='ip', rate='10/m', block=True)
def initiate_payment(request):
    if request.method != "POST":
        return render(request, 'quiz/play.html')

    player_name = request.POST.get('player_name', '').strip()
    phone_raw = request.POST.get('phone_number', '')
    email = request.POST.get('email', '').strip()
    
    phone = "".join(phone_raw.split()) if phone_raw else ""
    if not email:
        email = f"{phone}@trivia.com" if phone else "guest_player@trivia.com"

    amount = 20.00
    payment_uuid = str(uuid.uuid4())
    url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    
    callback_url = request.build_absolute_uri(reverse('quiz:paystack_callback'))
    
    payload = {
        "email": email,
        "amount": int(amount * 100), 
        "callback_url": callback_url,
        "reference": payment_uuid, 
        "metadata": {
            "player_name": player_name,
            "phone_number": phone
        }
    }
    
    subaccount_code = getattr(settings, 'PAYSTACK_SUBACCOUNT_CODE', None)
    if subaccount_code and subaccount_code.strip():
        payload["subaccount"] = subaccount_code
        payload["bearer"] = getattr(settings, 'PAYSTACK_BEARER', 'subaccount')
    
    try:
        response_raw = requests.post(url, json=payload, headers=headers)
        response = response_raw.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Paystack initialization network gateway error: {e}")
        return render(request, 'quiz/play.html', {'error': 'Payment gateway unreachable.'})
    
    if response.get('status'):
        PaystackTransaction.objects.create(
            paystack_reference=payment_uuid, 
            access_token=payment_uuid, 
            player_name=player_name,
            phone_number=phone,
            email=email,
            amount=amount,
            status='PENDING'
        )
        return redirect(response['data']['authorization_url'])
        
    logger.error(f"Paystack initialization failed: {response.get('message')}")
    return render(request, 'quiz/play.html', {'error': 'Failed to initialize payment transaction.'})


@ratelimit(key='ip', rate='10/m', block=True)
def paystack_callback(request):
    reference = request.GET.get('reference')
    if not reference:
        return redirect('quiz:payment_failed')

    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
    }
    
    try:
        response = requests.get(url, headers=headers).json()
    except Exception as e:
        logger.error(f"Callback verification gateway failure: {e}")
        return redirect('quiz:payment_failed')

    if response.get('status') and response['data']['status'] == 'success':
        tx = PaystackTransaction.objects.filter(paystack_reference=reference).first()
        if tx:
            tx.status = 'SUCCESS'
            metadata = response['data'].get('metadata', {})
            tx.player_name = metadata.get('player_name', tx.player_name)
            tx.phone_number = metadata.get('phone_number', tx.phone_number)
            tx.save()
            
            base_url = reverse('quiz:quiz_play')
            return redirect(f"{base_url}?token={tx.access_token}&reference={reference}")
            
    return redirect('quiz:payment_failed')


@ensure_csrf_cookie
def quiz_play_view(request):
    current_now = timezone.now()
    active_pool = QuizPool.objects.filter(
        is_active=True,
        start_time__lte=current_now,
        end_time__gte=current_now
    ).first()

    pool_id = active_pool.id if active_pool else 1
    target_timestamp_ms = int((timezone.now().timestamp() + GLOBAL_SESSION_LIMIT_SECONDS) * 1000)

    context = {
        'session_id': pool_id,
        'target_timestamp_ms': target_timestamp_ms,
        'duration_limit_seconds': int(GLOBAL_SESSION_LIMIT_SECONDS),
        'payment_status': 'PENDING',
        'transaction_reference': request.GET.get('reference', 'MPESA_REF_PENDING'),
        'access_token': request.GET.get('token', '')
    }
    return render(request, 'quiz/play.html', context)


@require_POST
@ratelimit(key='ip', rate='5/m', block=True)
def quiz_pay_view(request):
    try:
        if request.content_type == 'application/json':
            data = json.loads(request.body.decode('utf-8'))
            player_email = data.get('email', '').strip()
            phone_raw = data.get('phone_number', '')
            player_name = data.get('player_name', '').strip()
        else:
            player_email = request.POST.get('email', '').strip()
            phone_raw = request.POST.get('phone_number', '')
            player_name = request.POST.get('player_name', '').strip()
            
        phone = "".join(phone_raw.split()) if phone_raw else ""
        if not player_email:
            player_email = f"{phone}@trivia.com" if phone else "guest_player@trivia.com"

        paystack_url = "https://api.paystack.co/transaction/initialize"
        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "email": player_email,
            "amount": 2000, 
            "currency": "KES",
            "callback_url": request.build_absolute_uri(reverse('quiz:paystack_callback')),
            "metadata": {
                "player_name": player_name,
                "phone_number": phone
            },
            "subaccount": settings.PAYSTACK_SUBACCOUNT_CODE,
            "bearer": settings.PAYSTACK_BEARER
        }
        
        response = requests.post(paystack_url, json=payload, headers=headers)
        response_data = response.json()
        
        if response_data.get('status'):
            return JsonResponse({'status': 'SUCCESS', 'authorization_url': response_data['data']['authorization_url']})
            
        return JsonResponse({'status': 'FAILED', 'error': response_data.get('message', 'Initialization failure.')}, status=400)

    except Exception as e:
        logger.error(f"Error executing quiz_pay_view: {e}")
        return JsonResponse({'status': 'FAILED', 'error': 'Internal system transaction failure.'}, status=500)


def quiz_results_view(request):
    session_id = request.GET.get('session_id')
    if not session_id:
        return HttpResponse("Missing tracking context parameters.", status=400)

    session = GameSession.objects.filter(id=session_id).first()
    if not session:
        return HttpResponse("Session record could not be resolved.", status=404)

    context = {
        'score': session.score,
        'duration': round(session.duration_seconds, 2),
        'player_name': session.transaction_reference.player_name,
        'is_disqualified': session.is_disqualified,
        'pool_id': session.pool.id
    }
    return render(request, 'quiz/results.html', context)


@csrf_exempt
@require_POST
def paystack_webhook(request):
    signature = request.META.get('HTTP_X_PAYSTACK_SIGNATURE')
    if not signature:
        return HttpResponse("Missing security payload signature.", status=401)

    # Hardened inline HMAC-SHA512 verification logic
    computed_signature = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode('utf-8'),
        request.body,
        hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(computed_signature, signature):
        logger.warning("Rejected webhook attempt: Cryptographic payload signature mismatch.")
        return HttpResponse("Invalid cryptographic validation signature.", status=401)

    try:
        event_data = json.loads(request.body.decode('utf-8'))
    except ValueError:
        return HttpResponse("Invalid payload format.", status=400)

    if event_data.get('event') != 'charge.success':
        return JsonResponse({'status': 'ignored_event'})

    data = event_data.get('data', {})
    reference = data.get('reference')
    if not reference:
        return HttpResponse("Missing unique identifier reference.", status=400)

    amount_minor = Decimal(str(data.get('amount', 0)))
    amount_kes = amount_minor / Decimal('100.00')
    customer_email = data.get('customer', {}).get('email')
    
    authorization = data.get('authorization', {})
    gateway_sender_name = authorization.get('sender_name') or data.get('metadata', {}).get('player_name')
    raw_phone = data.get('customer', {}).get('phone') or data.get('metadata', {}).get('phone')
    
    player_name = gateway_sender_name if gateway_sender_name else "M-Pesa Guest Subscriber"
    fallback_phone = "".join(str(raw_phone).split()) if raw_phone else "00000000000"

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
@ratelimit(key='ip', rate='5/m', block=True)
def verify_user_payment(request):
    try:
        body = json.loads(request.body.decode('utf-8'))
        reference = body.get('reference')
    except (ValueError, KeyError, AttributeError):
        return JsonResponse({'success': False, 'status': 'FAILED', 'error': 'Malformed payload format.'}, status=400)

    if not reference:
        return JsonResponse({'success': False, 'status': 'FAILED', 'error': 'Reference parameter required.'}, status=400)

    verification_result = verify_paystack_transaction(reference)
    if not verification_result or verification_result.get('status') != 'SUCCESS':
        return JsonResponse({'success': False, 'status': 'FAILED', 'error': 'Verification failed at gateway.'}, status=422)

    amount_kes = verification_result.get('amount', Decimal('20.00'))
    prize_pool_share = amount_kes * Decimal('0.80')

    customer_email = verification_result.get('email')
    gateway_sender_name = verification_result.get('sender_name') or verification_result.get('metadata', {}).get('player_name')
    raw_phone = verification_result.get('phone') or verification_result.get('metadata', {}).get('phone')
    
    player_name = gateway_sender_name if gateway_sender_name else "M-Pesa Guest Subscriber"
    fallback_phone = "".join(str(raw_phone).split()) if raw_phone else "00000000000"

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
            return JsonResponse({'success': True, 'status': 'SUCCESS', 'access_token': str(tx.access_token)})

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
            active_pool.process_incoming_entry(entry_fee=prize_pool_share)

    return JsonResponse({'success': True, 'status': 'SUCCESS', 'access_token': str(tx.access_token)})


@require_POST
def start_quiz_session(request):
    try:
        body = json.loads(request.body.decode('utf-8'))
        token_str = body.get('access_token') or body.get('token') or body.get('reference')
    except (ValueError, AttributeError):
        return JsonResponse({'success': False, 'error': 'Malformed request body.'}, status=400)

    if not token_str:
        return JsonResponse({'success': False, 'error': 'Access token required.'}, status=400)

    current_now = timezone.now()
    current_pool = QuizPool.objects.filter(
        is_active=True,
        start_time__lte=current_now,
        end_time__gte=current_now
    ).first()

    if not current_pool:
        current_pool = QuizPool.objects.filter(is_active=True).first()
        if not current_pool:
            current_pool = QuizPool.objects.create(
                start_time=current_now - timezone.timedelta(minutes=5),
                end_time=current_now + timezone.timedelta(hours=3),
                is_active=True,
                grand_prize_pool=Decimal('0.00'),
                total_entries=0
            )

    all_question_ids = list(QuestionBank.objects.filter(pool=current_pool).values_list('id', flat=True))
    if len(all_question_ids) < 10:
        return JsonResponse({'success': False, 'error': 'Insufficient questions in the active pool.'}, status=400)

    with transaction.atomic():
        tx_query = PaystackTransaction.objects.select_for_update().filter(
            Q(access_token=token_str) | Q(paystack_reference=token_str)
        )
        
        tx = tx_query.filter(status='SUCCESS').first()
        if not tx:
            tx = tx_query.first()
            if tx:
                tx.status = 'SUCCESS'
                tx.save()

        if not tx:
            return JsonResponse({'success': False, 'error': 'Invalid access token or transaction missing.'}, status=404)
            
        if getattr(tx, 'is_token_used', False):
            return JsonResponse({'success': False, 'error': 'This payment token has already been used.'}, status=403)

        tx.is_token_used = True
        tx.save()

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
        'player_name': getattr(tx, 'player_name', 'Player')
    })


@require_POST
@ratelimit(key='post:session_id', rate='1/m', block=True)
def submit_quiz_answers(request):
    try:
        body = json.loads(request.body.decode('utf-8'))
        session_id = body.get('session_id')
        player_answers = body.get('answers')
    except (ValueError, AttributeError):
        return JsonResponse({'success': False, 'error': 'Malformed request JSON.'}, status=400)

    session = GameSession.objects.filter(id=session_id, end_time__isnull=True).first()
    if not session:
        return JsonResponse({'success': False, 'error': 'Invalid or already closed session.'}, status=404)

    now = timezone.now()
    session.end_time = now
    duration = (now - session.start_time).total_seconds()
    session.duration_seconds = round(duration, 2)

    max_allowed_time = GLOBAL_SESSION_LIMIT_SECONDS + LATENCY_GRACE_WINDOW_SECONDS
    if duration > max_allowed_time:
        session.is_disqualified = True
        session.score = 0
        session.save()
        
        tx = session.transaction_reference
        if tx:
            tx.score = 0
            tx.time_taken = session.duration_seconds
            tx.save()
            
        return JsonResponse({
            'success': False, 
            'error': f'Disqualified: Evaluation timeout at {session.duration_seconds}s.'
        }, status=400)

    calculated_score = 0
    if isinstance(player_answers, str):
        try:
            player_answers = json.loads(player_answers)
        except json.JSONDecodeError:
            player_answers = {}

    pool_questions = {str(q.id): q for q in QuestionBank.objects.filter(pool=session.pool)}

    if isinstance(player_answers, dict):
        for q_id, chosen_idx in player_answers.items():
            q_id = str(q_id)
            chosen_idx = str(chosen_idx).strip()

            if q_id in pool_questions:
                question = pool_questions[q_id]
                if chosen_idx == str(question.correct_choice).strip():
                    calculated_score += 1

    session.score = calculated_score
    session.save()

    tx = session.transaction_reference
    if tx:
        tx.score = calculated_score
        tx.time_taken = session.duration_seconds
        tx.save()

    return JsonResponse({
        'success': True,
        'score': session.score,
        'duration_seconds': session.duration_seconds
    })