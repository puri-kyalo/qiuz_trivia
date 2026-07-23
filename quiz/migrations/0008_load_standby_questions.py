from django.db import migrations
from django.core.management import call_command


def load_standby_data(apps, schema_editor):
    call_command('loaddata', 'standby_questions.json')


def unload_standby_data(apps, schema_editor):
    StandbyQuestion = apps.get_model('quiz', 'StandbyQuestion')
    StandbyQuestion.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('quiz', '0007_remove_questionbank_valid_choice_index_range_and_more'),
    ]

    operations = [
        migrations.RunPython(load_standby_data, reverse_code=unload_standby_data),
    ]