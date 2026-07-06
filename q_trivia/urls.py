from django.contrib import admin
from django.urls import path, include
from quiz import views as quiz_views 

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # 1. Mount your quiz endpoints with an explicit application namespace
    path('quiz/', include(('quiz.urls', 'quiz'), namespace='quiz')),
    
    # 2. Point the root domain to the template renderer view
    path('', quiz_views.quiz_play_view, name='home'),
]