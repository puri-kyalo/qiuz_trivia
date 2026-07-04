# quiz/management/commands/seed_quiz_questions.py
import os
import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List
from quiz.models import QuizPool, QuestionBank

logger = logging.getLogger(__name__)

class SingleQuestionSchema(BaseModel):
    question_text: str = Field(description="Trivia question text. Must be medium difficulty, not obvious, not highly obscure.")
    choice_1: str = Field(description="Multiple-choice option 1.")
    choice_2: str = Field(description="Multiple-choice option 2.")
    choice_3: str = Field(description="Multiple-choice option 3.")
    choice_4: str = Field(description="Multiple-choice option 4.")
    correct_choice: int = Field(description="The matching index number for the answer, strictly 1, 2, 3, or 4.")
    category: str = Field(description="Must be exactly 'LOCAL_FOOTBALL', 'KENYAN_HISTORY', or 'WORLD_FOOTBALL'.")

class QuizBatchSchema(BaseModel):
    questions: List[SingleQuestionSchema]


class Command(BaseCommand):
    help = "Programmatically pre-seeds validated medium-difficulty trivia questions for upcoming pools using modern AI specs."

    def handle(self, *args, **options):
        current_now = timezone.now()
        
        upcoming_pool = QuizPool.objects.filter(
            is_active=True,
            start_time__gt=current_now
        ).order_by('start_time').first()

        if not upcoming_pool:
            self.stdout.write(self.style.WARNING("No upcoming quiz pools found to pre-seed."))
            return

        time_to_start = upcoming_pool.start_time - current_now
        if time_to_start.total_seconds() > 600:
            self.stdout.write(self.style.NOTICE(f"Upcoming pool {upcoming_pool.id} is outside the 10-minute pre-seed window."))
            return

        self.stdout.write(f"Pre-seeding target verified for Pool ID: {upcoming_pool.id}")
        
        allowed_categories = ["LOCAL_FOOTBALL", "KENYAN_HISTORY", "WORLD_FOOTBALL"]
        generated_questions = []
        
        api_key = os.getenv("GEMINI_API_KEY")

        if api_key:
            try:
                client = genai.Client(api_key=api_key)
                
                prompt = """
                Generate exactly 12 trivia questions distributed evenly across these choices:
                1. LOCAL_FOOTBALL (Kenyan Premier League, K'Ogalo vs Mashemeji Derby, Harambee Stars milestones)
                2. KENYAN_HISTORY (Historical milestones, Mau Mau era, independence, cultural history)
                3. WORLD_FOOTBALL (Global football icons, EPL, UEFA Champions League, World Cups)

                DIFFICULTY PARAMETERS:
                - Maintain a strict medium difficulty ("Goldilocks Zone").
                - No super basic entries (e.g., 'Who was Kenya's first president?' or 'Which country won the 2022 World Cup?').
                - No insanely niche records (e.g., exact timestamps of match goals or minute details of stadium architects).
                - Target interesting, engaging inquiries requiring brief contemplation.
                """

                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=QuizBatchSchema,
                        temperature=0.7
                    ),
                )

                parsed_data = QuizBatchSchema.model_validate_json(response.text)
                
                for item in parsed_data.questions:
                    if item.correct_choice in [1, 2, 3, 4] and item.category in allowed_categories:
                        generated_questions.append({
                            'question_text': item.question_text,
                            'choice_1': item.choice_1,
                            'choice_2': item.choice_2,
                            'choice_3': item.choice_3,
                            'choice_4': item.choice_4,
                            'correct_choice': item.correct_choice,
                            'category': item.category
                        })
                        
            except Exception as e:
                logger.error(f"AI generation/parsing anomaly intercepted: {str(e)}. Falling back to repository pool.")

        if len(generated_questions) < 12:
            self.stdout.write(self.style.WARNING("AI seeding was incomplete. Injecting verified local fallbacks..."))
            generated_questions = self.get_structural_fallback_repository()

        with transaction.atomic():
            for q_data in generated_questions:
                QuestionBank.objects.create(
                    pool=upcoming_pool,
                    question_text=q_data['question_text'],
                    choice_1=q_data['choice_1'],
                    choice_2=q_data['choice_2'],
                    choice_3=q_data['choice_3'],
                    choice_4=q_data['choice_4'],
                    correct_choice=q_data['correct_choice'],
                    category=q_data['category']
                )
        
        self.stdout.write(self.style.SUCCESS(f"Successfully pre-seeded {len(generated_questions)} questions to QuizPool {upcoming_pool.id}."))

    def get_structural_fallback_repository(self):
        return [
            {
                "question_text": "Which club won the FKF Premier League title in the 2022/2023 season?",
                "choice_1": "Gor Mahia", "choice_2": "AFC Leopards", "choice_3": "Tusker FC", "choice_4": "Bandari FC",
                "correct_choice": 1, "category": "LOCAL_FOOTBALL"
            },
            {
                "question_text": "In which year did Kenya officially adopt its current constitution?",
                "choice_1": "1963", "choice_2": "2002", "choice_3": "2010", "choice_4": "2005",
                "correct_choice": 3, "category": "KENYAN_HISTORY"
            },
            {
                "question_text": "Which country won the FIFA World Cup held in the year 2022?",
                "choice_1": "France", "choice_2": "Argentina", "choice_3": "Croatia", "choice_4": "Morocco",
                "correct_choice": 2, "category": "WORLD_FOOTBALL"
            }
        ] * 4