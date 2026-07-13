from django.contrib import admin
from django.urls import path, include
from quiz import views as quiz_views 

urlpatterns = [
    path('admin/', admin.site.urls),
    path('quiz/', include(('quiz.urls', 'quiz'), namespace='quiz')),
    path('', quiz_views.quiz_play_view, name='home'),
]