# quiz/urls.py
from django.urls import path
from . import views

# REGISTER THE APP NAMESPACE
app_name = 'quiz'

urlpatterns = [
    # --- Structural/Frontend Flow Views ---
    path('', views.landing_page, name='landing'),                                # 🔥 FIXED: The true entry page that initiates the form tokens safely
    path('pay/', views.initiate_payment, name='initiate_payment'),               # Handles form submissions securely
    path('pay/callback/', views.paystack_callback, name='paystack_callback'),     # Paystack callback processing
    path('play/', views.quiz_play_view, name='quiz_play'),                       # The live question layout page
    path('results/', views.quiz_results_view, name='quiz_results'),              # 🔥 FIXED: Un-commented so results show smoothly with zero 404s

    # --- Backend/Webhook Infrastructure Paths ---
    path('webhook/paystack/', views.paystack_webhook, name='paystack_webhook'),
    path('payment/verify/', views.verify_user_payment, name='verify_user_payment'),
    path('game/start/', views.start_quiz_session, name='start_quiz_session'),
    path('game/submit/', views.submit_quiz_answers, name='submit_quiz_answers'),

    # --- Frontend JavaScript API Routing Engine Aliases ---
    path('verify-payment/', views.verify_user_payment, name='js_verify_payment'),
    path('start-session/', views.start_quiz_session, name='js_start_session'),
    path('submit-answers/', views.submit_quiz_answers, name='js_submit_answers'),
    path('api/start-session/', views.start_quiz_session, name='start_session'),
]