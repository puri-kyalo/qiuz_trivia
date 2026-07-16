import os
import time
import logging
from datetime import timedelta
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
    question_text: str = Field(description="Trivia question text. Target medium difficulty.")
    choice_1: str = Field(description="Multiple-choice option 1.")
    choice_2: str = Field(description="Multiple-choice option 2.")
    choice_3: str = Field(description="Multiple-choice option 3.")
    choice_4: str = Field(description="Multiple-choice option 4.")
    correct_choice: int = Field(description="Correct choice index: 1, 2, 3, or 4.")
    category: str = Field(description="Must be exactly 'LOCAL_FOOTBALL', 'KENYAN_HISTORY', or 'WORLD_FOOTBALL'.")

class QuizBatchSchema(BaseModel):
    questions: List[SingleQuestionSchema]


class Command(BaseCommand):
    help = "Seeds trivia questions into the active quiz pool using Gemini"

    def handle(self, *args, **options):
        current_now = timezone.now()
        
        target_pool = QuizPool.objects.filter(is_active=True).order_by('-id').first()
        if not target_pool:
            self.stdout.write(self.style.WARNING("No active pool found. Creating fallback pool..."))
            target_pool = QuizPool.objects.create(
                is_active=True,
                start_time=current_now - timedelta(days=1),
                end_time=current_now + timedelta(days=7)
            )

        self.stdout.write(f"Seeding questions for Pool ID: {target_pool.id}")
        
        allowed_categories = {"LOCAL_FOOTBALL", "KENYAN_HISTORY", "WORLD_FOOTBALL"}
        generated_questions = []
        api_key = os.getenv("GEMINI_API_KEY")

        if api_key:
            try:
                client = genai.Client(api_key=api_key)
                # 3 batches of 20 gives us 60 questions, which keeps us safely under the 5 RPM limit
                total_batches = 3
                questions_per_batch = 20
                
                for batch_idx in range(total_batches):
                    # Sleep 12 seconds between batches to avoid the Gemini free tier 429 rate limit
                    if batch_idx > 0:
                        self.stdout.write("Waiting 12 seconds to respect API rate limits...")
                        time.sleep(12)

                    self.stdout.write(f"Fetching batch {batch_idx + 1} of {total_batches}...")
                    
                    prompt = f"""
                    Generate exactly {questions_per_batch} unique trivia questions distributed evenly across:
                    1. LOCAL_FOOTBALL (Kenyan Premier League, Mashemeji Derby, Harambee Stars milestones)
                    2. KENYAN_HISTORY (Historical milestones, independence era, cultural history)
                    3. WORLD_FOOTBALL (Global icons, Premier League, UEFA Champions League, World Cups)

                    Requirements:
                    - Strict medium difficulty level.
                    - Ensure questions are factually accurate and unique.
                    """

                    response = client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=QuizBatchSchema,
                            temperature=0.7,
                        ),
                    )

                    parsed_data = response.parsed
                    batch_count = 0
                    
                    if parsed_data and hasattr(parsed_data, 'questions'):
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
                                batch_count += 1
                    
                    self.stdout.write(self.style.SUCCESS(f"Collected {batch_count} valid records from batch {batch_idx + 1}."))
                        
            except Exception as e:
                logger.error(f"Gemini generation error: {str(e)}")
                self.stdout.write(self.style.ERROR(f"API generation failed: {str(e)}"))

        # Use fallback questions if generation fails or API key is missing
        if not generated_questions:
            self.stdout.write(self.style.WARNING("No questions generated. Using fallback questions..."))
            generated_questions = self.get_fallback_questions() * 34 

        # Bulk write to database
        self.stdout.write("Saving questions to database...")
        with transaction.atomic():
            saved_counter = 0
            for q_data in generated_questions:
                QuestionBank.objects.create(
                    pool=target_pool,
                    question_text=q_data['question_text'],
                    choice_1=q_data['choice_1'],
                    choice_2=q_data['choice_2'],
                    choice_3=q_data['choice_3'],
                    choice_4=q_data['choice_4'],
                    correct_choice=q_data['correct_choice'],
                    category=q_data['category']
                )
                saved_counter += 1
                if saved_counter >= 100:
                    break
        
        self.stdout.write(self.style.SUCCESS(f"Successfully populated {saved_counter} questions into Pool {target_pool.id}."))

    def get_fallback_questions(self):
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
        ]