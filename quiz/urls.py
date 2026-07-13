from django.urls import path
from . import views

app_name = 'quiz'

urlpatterns = [
    path('', views.landing_page, name='landing'),
    path('pay/', views.initiate_payment, name='initiate_payment'),
    path('pay/callback/', views.paystack_callback, name='paystack_callback'),
    path('play/', views.quiz_play_view, name='quiz_play'),
    path('results/', views.quiz_results_view, name='quiz_results'),

    path('webhook/paystack/', views.paystack_webhook, name='paystack_webhook'),
    path('payment/verify/', views.verify_user_payment, name='verify_user_payment'),
    path('game/start/', views.start_quiz_session, name='start_quiz_session'),
    path('game/submit/', views.submit_quiz_answers, name='submit_quiz_answers'),

    path('verify-payment/', views.verify_user_payment, name='js_verify_payment'),
    path('start-session/', views.start_quiz_session, name='js_start_session'),
    path('submit-answers/', views.submit_quiz_answers, name='js_submit_answers'),
    path('api/start-session/', views.start_quiz_session, name='start_session'),
]