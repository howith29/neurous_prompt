"""Fan out one prepared article into three independent generation branches."""
import uuid
from langchain_core.runnables import RunnableLambda, RunnableParallel
from .pipeline_core import split_gen_output, calc_stats, check_constraints
from .quiz_order import QuizOrder

LEVELS = ('BEGINNER', 'INTERMEDIATE', 'ADVANCED')

class ParallelGeneration:
    def __init__(self, pipeline, max_regen=2):
        self.pipeline = pipeline
        self.max_regen = max_regen
        self.quiz_order = QuizOrder()

    def run(self, prepared, on_event=None, on_result=None, on_quiz=None):
        if prepared.get('status') != 'ready':
            return {}
        emit = on_event or (lambda **event: None)
        save = on_result or (lambda result: None)

        save_quiz = on_quiz or (lambda result: None)

        def branch(level):
            def generate(article):
                emit(stage='generation', level=level, status='started')
                row = {'generation_id': str(uuid.uuid4()), 'news_id': article['content']['news_id'], 'level': level}
                row['content_kind'] = article.get('filter', {}).get('content_kind', 'UNKNOWN')
                try:
                    result = self.pipeline.run(
                        title=article['content']['title'], content=article['content']['text'],
                        category=article['classification']['final_category'], level=level,
                        max_regen=self.max_regen,
                        prepared=article,
                    )
                    if result.error or not result.passed_filter:
                        row.update(status='error', error='generation_failed')
                    else:
                        title, body = split_gen_output(result.content)
                        status = 'success'
                        stats = calc_stats(body)
                        checks = check_constraints(stats, level)
                        checks.update(title_ok=bool(title), ending_ok=body.rstrip().endswith('.'))
                        validation = dict(stats=stats, checks=checks, passed=all(checks.values()))
                        if not validation['passed']: status = 'validation_failed'
                        emit(stage='validation', level=level, status='passed' if validation['passed'] else 'failed', result=validation)
                        row['validation'] = validation
                        if not title or not body: status = 'error'
                        row.update(status=status, title=title, text=body,
                                   regen_count=result.regen_count)
                except Exception as exc:
                    row.update(status='error', error=type(exc).__name__)
                # Persist each completed branch even if another branch is still running.
                save(row)
                emit(stage='generation', level=level, status=row['status'])
                quiz_row = {k: row[k] for k in ('generation_id', 'news_id', 'level')}
                if row['status'] != 'success':
                    quiz_row.update(status='skipped', reason='generation_not_successful')
                else:
                    emit(stage='quiz', level=level, status='started')
                    try:
                        quiz_result = self.pipeline.generate_quiz(
                            category=article['classification']['final_category'], level=level,
                            content_text=f"제목: {row['title']}\n\n{row['text']}",
                        )
                        if quiz_result.error or quiz_result.quiz is None:
                            quiz_row.update(status='error', error='quiz_generation_failed')
                        else:
                            quiz_row.update(status='success', quiz=self.quiz_order.arrange(quiz_result.quiz.model_dump()))
                    except Exception as exc:
                        quiz_row.update(status='error', error=type(exc).__name__)
                save_quiz(quiz_row)
                emit(stage='quiz', level=level, status=quiz_row['status'])
                row['quiz_result'] = quiz_row
                return row
            return RunnableLambda(generate)

        return RunnableParallel(**{level: branch(level) for level in LEVELS}).invoke(
            prepared, config={'max_concurrency': 3})
