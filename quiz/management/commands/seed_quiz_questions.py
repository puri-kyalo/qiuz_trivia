import json
import logging
import time
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.db.models import F
from django.conf import settings

from quiz.models import QuizPool, QuestionBank, StandbyQuestion

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Seeds quiz questions into the active QuizPool or directly into the StandbyQuestion table."

    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=200,
            help='Number of questions required.'
        )
        parser.add_argument(
            '--standby',
            action='store_true',
            help='Seed questions directly into the StandbyQuestion bank without assigning them to an active pool.'
        )

    def handle(self, *args, **options):
        count = options['count']
        is_standby = options['standby']

        # =========================================================
        # BRANCH 1: Populate the Standby Bank directly
        # =========================================================
        if is_standby:
            self.stdout.write(self.style.NOTICE(f"Seeding {count} questions directly into StandbyQuestion table..."))
            
            questions_data = self._fetch_remote_questions(count)
            if not questions_data:
                self.stdout.write(self.style.ERROR("Failed to fetch remote questions for standby bank."))
                return

            with transaction.atomic():
                created_standby = StandbyQuestion.objects.bulk_create([
                    StandbyQuestion(
                        question_text=q['question_text'],
                        choice_1=q['choice_1'],
                        choice_2=q['choice_2'],
                        choice_3=q['choice_3'],
                        choice_4=q['choice_4'],
                        correct_choice=str(q['correct_choice']),
                        category=q.get('category', 'General'),
                        times_used=0
                    )
                    for q in questions_data
                ])

            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully added {len(created_standby)} questions to StandbyQuestion bank! "
                    f"Total standby pool count: {StandbyQuestion.objects.count()}"
                )
            )
            return

        # =========================================================
        # BRANCH 2: Standard Active Pool Seeding (Existing Logic)
        # =========================================================
        now = timezone.now()

        # Always target the latest active pool by ordering descending by ID
        active_pool = QuizPool.objects.filter(
            is_active=True,
            start_time__lte=now,
            end_time__gte=now
        ).order_by('-id').first()

        # Fallback if no active pool covers the current timestamp
        if not active_pool:
            # Ensure older active pools are deactivated first
            QuizPool.objects.filter(is_active=True).update(is_active=False)
            
            active_pool = QuizPool.objects.create(
                start_time=now,
                end_time=now + timezone.timedelta(hours=3),  # Aligned to 3-hour rotation
                is_active=True,
                grand_prize_pool=Decimal('0.00'),
                total_entries=0
            )
            self.stdout.write(
                self.style.WARNING(f"No active pool found. Created new Pool #{active_pool.id}.")
            )

        # 1. Attempt remote fetch via Gemini
        questions_data = self._fetch_remote_questions(count)

        # 2. Top-up or fallback using standby database if remote fetch didn't yield enough
        if not questions_data or len(questions_data) < count:
            existing_count = len(questions_data) if questions_data else 0
            needed = count - existing_count

            self.stdout.write(
                self.style.WARNING(
                    f"Remote API provided {existing_count}/{count} questions. "
                    f"Pulling remaining {needed} from standby database..."
                )
            )

            standby_questions = self._get_db_standby(needed)
            if questions_data:
                questions_data.extend(standby_questions)
            else:
                questions_data = standby_questions

        if not questions_data:
            self.stdout.write(
                self.style.ERROR("No standby questions found in database. Aborting.")
            )
            return

        # 3. Bulk insert questions into QuestionBank
        with transaction.atomic():
            created_objects = QuestionBank.objects.bulk_create([
                QuestionBank(
                    pool=active_pool,
                    question_text=q['question_text'],
                    choice_1=q['choice_1'],
                    choice_2=q['choice_2'],
                    choice_3=q['choice_3'],
                    choice_4=q['choice_4'],
                    correct_choice=str(q['correct_choice']),
                    category=q.get('category', 'General')
                )
                for q in questions_data
            ])

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully added {len(created_objects)} questions to Pool #{active_pool.id}."
            )
        )

    def _fetch_remote_questions(self, target_count):
        api_key = getattr(settings, 'GEMINI_API_KEY', None)
        if not api_key:
            return None

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            all_questions = []
            batch_size = 50

            while len(all_questions) < target_count:
                remaining = target_count - len(all_questions)
                current_batch = min(batch_size, remaining)

                prompt = f"""
                Generate exactly {current_batch} unique multiple choice trivia questions in raw JSON format.

                CATEGORIES & REQUIREMENTS:
                1. Split questions evenly across these three categories only:
                   - "Local Kenyan Football" (FKF Premier League, Harambee Stars, Gor Mahia, AFC Leopards, CECAFA context)
                   - "Kenyan History" (Struggle for independence, historic political events, leaders, constitutional milestones)
                   - "World General Football" (FIFA World Cup, UEFA Champions League, international stars, historic club records)

                2. DIFFICULTY:
                   - STRICT MEDIUM DIFFICULTY. Target trivia enthusiasts — avoid basic questions and obscure niche facts.

                3. JSON FORMAT:
                [
                  {{
                    "question_text": "Which club won the FKF Premier League title in 2024?",
                    "choice_1": "Gor Mahia",
                    "choice_2": "AFC Leopards",
                    "choice_3": "Tusker FC",
                    "choice_4": "Police FC",
                    "correct_choice": "1",
                    "category": "Local Kenyan Football"
                  }}
                ]

                Rules:
                - correct_choice must be a string ("1", "2", "3", or "4").
                - category must be one of: "Local Kenyan Football", "Kenyan History", or "World General Football".
                """

                # Retry loop handling transient API spikes (e.g. 503 errors)
                max_retries = 3
                batch_success = False

                for attempt in range(max_retries):
                    try:
                        response = client.models.generate_content(
                            model='gemini-2.5-flash',
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                temperature=0.7,
                            ),
                        )

                        if response and response.text:
                            data = json.loads(response.text)
                            if isinstance(data, list):
                                all_questions.extend(data)
                                batch_success = True
                                break  # Break retry loop on success
                    except Exception as e:
                        wait_time = (attempt + 1) * 2
                        if "503" in str(e) or "UNAVAILABLE" in str(e):
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Gemini API high demand spike (503). Retrying in {wait_time}s... (Attempt {attempt + 1}/{max_retries})"
                                )
                            )
                        else:
                            logger.warning(f"Error fetching questions: {str(e)}. Retrying in {wait_time}s...")
                        
                        time.sleep(wait_time)

                # If all retries failed for a batch, break out to let the standby fallback handle the rest
                if not batch_success:
                    self.stdout.write(self.style.ERROR("API batch failed after retries. Stopping remote fetch."))
                    break

            return all_questions[:target_count] if all_questions else None

        except Exception as e:
            logger.error(f"Failed to fetch remote questions: {str(e)}")

        return None

    def _get_db_standby(self, count):
        standby_qs = StandbyQuestion.objects.order_by('times_used', '?')[:count]

        if standby_qs.count() < count:
            self.stdout.write(
                self.style.ERROR(
                    f"Warning: Standby table has {standby_qs.count()} questions (requested {count})."
                )
            )

        questions = []
        standby_ids = []

        for q in standby_qs:
            questions.append({
                'question_text': q.question_text,
                'choice_1': q.choice_1,
                'choice_2': q.choice_2,
                'choice_3': q.choice_3,
                'choice_4': q.choice_4,
                'correct_choice': q.correct_choice,
                'category': q.category,
            })
            standby_ids.append(q.id)

        if standby_ids:
            StandbyQuestion.objects.filter(id__in=standby_ids).update(
                times_used=F('times_used') + 1
            )

        return questions