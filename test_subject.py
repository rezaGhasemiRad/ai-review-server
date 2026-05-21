"""Sample file with intentional issues for testing review mode."""
import threading


class Counter:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def increment(self, n=1):
        # BUG: forgot to acquire the lock — race condition
        self.value = self.value + n

    def reset(self):
        with self.lock:
            self.value = 0


def find_user(users, target_id):
    # BUG: returns the LAST match's index, not the first; also uses == on possibly-None
    found = -1
    for i in range(len(users)):
        if users[i]["id"] == target_id:
            found = i
    return found


def parse_age(raw):
    # SMELL: silent except swallows everything, including KeyboardInterrupt
    try:
        return int(raw)
    except:
        return None


def divide_all(numbers, divisor):
    # EDGE CASE: no handling for divisor == 0
    return [n / divisor for n in numbers]
