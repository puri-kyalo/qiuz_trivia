# quiz/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('webhook/paystack/', views.paystack_webhook, name='paystack_webhook'),
    path('payment/verify/', views.verify_user_payment, name='verify_user_payment'),

    # Core Gameplay System Paths (Phase 3)
    path('game/start/', views.start_quiz_session, name='start_quiz_session'),
    path('game/submit/', views.submit_quiz_answers, name='submit_quiz_answers'),
]