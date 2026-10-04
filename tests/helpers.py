"""Small deterministic fakes; never connect to the network or launch programs."""

from collections import deque
import json


class FakeModel:
    def __init__(self, *outcomes):
        self.outcomes = deque(outcomes)
        self.calls = []

    def enqueue(self, outcome):
        self.outcomes.append(outcome)

    def complete(self, messages, schemas, token):
        token.check()
        self.calls.append((tuple(messages), tuple(schemas)))
        if not self.outcomes:
            raise AssertionError("Unexpected model call")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeEvents:
    def __init__(self):
        self.events = []

    def record(self, event_type, data=None):
        self.events.append((event_type, data))

    def count(self, event_type):
        return sum(name == event_type for name, _ in self.events)


class FakeTransport:
    """Scripted HTTP transport: any unscripted request fails before reaching a socket."""

    def __init__(self, *responses):
        self.responses = deque(responses)
        self.requests = []

    def enqueue(self, response):
        self.responses.append(response)

    def send(self, url, headers, bodybytes, timeout_seconds, token):
        token.check()
        self.requests.append((url, dict(headers), bytes(bodybytes), timeout_seconds))
        if not self.responses:
            raise AssertionError(f"Unexpected HTTP request: {url}")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


class FakeToolRegistry:
    """Approved fake action: records intent without any operating-system side effect."""

    result = "FAKE_APP_OPENED_ONCE"

    def __init__(self, on_execute=None):
        self.schemas = ()
        self.executed = []
        self.on_execute = on_execute

    def validate(self, call):
        try:
            arguments = json.loads(call.arguments_json)
        except (ValueError, TypeError):
            return "Invalid fake arguments"
        if call.name == "open_app" and isinstance(arguments, dict) and \
                set(arguments) == {"app"} and isinstance(arguments["app"], str) and \
                arguments["app"].casefold() in {"notepad", "calculator"}:
            return None
        return "Fake registry rejects this call"

    def requires_confirmation(self, call):
        return True

    def describe(self, call):
        return "Fake target " + json.loads(call.arguments_json)["app"]

    def execute(self, call, token):
        token.check()
        error = self.validate(call)
        if error:
            raise AssertionError(error)
        self.executed.append(call)
        if self.on_execute is not None:
            self.on_execute()
        return self.result
