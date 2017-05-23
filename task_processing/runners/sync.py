import time

from six.moves.queue import Queue

from task_processing.interfaces.runner import Runner


class Sync(Runner):
    def __init__(self, executor):
        self.executor = executor
        self.TASK_CONFIG_INTERFACE = executor.TASK_CONFIG_INTERFACE
        self.queue = Queue()

    def kill(self, *args):
        pass

    def run(self, task_config):
        self.executor.run(task_config)
        event_queue = self.executor.get_event_queue()

        while True:
            event = event_queue.get()
            if event.task_id != task_config.task_id:
                event_queue.put(event)
                time.sleep(1)  # hope somebody else picks it up?
                continue

            if event.terminal:
                return event
            else:
                print("Non-terminal event: %s", event)

    def stop(self):
        self.executor.stop()
