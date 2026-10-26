"""Balance correct positions per execution, preserving the correct answer text."""
import random
import threading


class QuizOrder:
    def __init__(self):
        self._lock = threading.Lock()
        self._positions = []

    def arrange(self, quiz):
        choices = list(quiz['choices'])
        answer = quiz['answerNo']
        if len(choices) != 3 or answer not in (1, 2, 3) or len(set(choices)) != 3:
            raise ValueError('Invalid or duplicate choices')
        correct = choices.pop(answer - 1)
        random.shuffle(choices)
        with self._lock:
            if not self._positions:
                self._positions = [1, 2, 3]
                random.shuffle(self._positions)
            target = self._positions.pop()
        choices.insert(target - 1, correct)
        return {**quiz, 'choices': choices, 'answerNo': target}
