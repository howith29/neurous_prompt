import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from new_pipeline.backend.quiz_order import QuizOrder


class QuizOrderTests(unittest.TestCase):
    def test_parallel_balance_and_answer_preserved(self):
        order = QuizOrder()
        inputs = [dict(question='질문', choices=['가', '나', '다'], answerNo=i % 3 + 1) for i in range(87)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            outputs = list(pool.map(order.arrange, inputs))
        self.assertEqual(Counter(q['answerNo'] for q in outputs), {1:29, 2:29, 3:29})
        for before, after in zip(inputs, outputs):
            self.assertEqual(before['choices'][before['answerNo']-1], after['choices'][after['answerNo']-1])
            self.assertEqual(before['choices'], ['가', '나', '다'])

    def test_reject_duplicate_choices(self):
        with self.assertRaises(ValueError):
            QuizOrder().arrange(dict(choices=['가', '가', '다'], answerNo=1))
