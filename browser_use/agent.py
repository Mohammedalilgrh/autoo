import time

class Agent:
    def __init__(self, task=None, llm=None, browser=None):
        self.task = task
        self.llm = llm
        self.browser = browser
        self.actions = []  # mouse/scroll actions
        self.inputs = []   # fields to type in

    def run_sync(self, timeout=300):
        """Run the task and populate actions/inputs."""
        time.sleep(1)  # simulate processing

        if self.task:
            if "scroll" in self.task.lower():
                self.actions.append({"type": "scroll", "amount": 300})
            if "click" in self.task.lower():
                self.actions.append({"type": "click", "selector": "body"})
            if "type" in self.task.lower():
                self.inputs.append({"selector": "input", "value": "Test"})
        return f"Task '{self.task}' executed successfully"
