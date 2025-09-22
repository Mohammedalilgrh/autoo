class ChatGoogle:
    def __init__(self, model=None, api_key=None):
        self.model = model
        self.api_key = api_key

    def run(self, prompt):
        return f"LLM simulated response: {prompt}"
