# q_trivia/urls.py
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/quiz/', include('quiz.urls')),
]