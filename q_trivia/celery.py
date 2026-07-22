import os
from celery import Celery

# Set the default Django settings module for 'celery'
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'q_trivia.settings')

app = Celery('q_trivia')

# Read config from Django settings, using 'CELERY_' prefix
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered app configs
app.autodiscover_tasks()