"""Coordinator safety/state tests. No operating-system effects are performed."""

import unittest
from concurrent.futures import Future
from threading import Event, Thread

from envi.assistant import AssistantService
from envi.cancellation import CancellationToken
from envi.configuration import AppSettings
from envi.errors import AssistantError, CancelledError
from envi.models import ModelReply, ToolCall
from envi.routing import RuleRouter
from envi.tools import ToolRegistry

from tests.helpers import FakeEvents, FakeModel, FakeToolRegistry


def reply_with(*calls):
    return ModelReply(None, tuple(calls), "tool_calls")


class AssistantTests(unittest.TestCase):
    def service(self, model=None, settings=None, tools=None):
        self.settings = settings or AppSettings()
        self.model = model or FakeModel()
        self.tools = tools or ToolRegistry(self.settings)
        self.events = FakeEvents()
        return AssistantService(self.model, self.tools, RuleRouter(), self.settings, self.events)

    def assert_history(self, service, question, answer):
        history = service.get_history_snapshot()
        self.assertEqual(2, len(history))
        self.assertEqual(("user", question), (history[0].role, history[0].content))
        self.assertEqual(("assistant", answer), (history[1].role, history[1].content))

    def reject_approval(self, call):
        self.fail("Unexpected approval: " + call.name)

    def start_request(self, service, question, token=None):
        outcome = Future()

        def run():
            try:
                outcome.set_result(service.process(question, self.reject_approval, token or CancellationToken()))
            except Exception as error:
                outcome.set_exception(error)

        thread = Thread(target=run, daemon=True)
        thread.start()
        return thread, outcome

    def test_local_time_never_calls_model_or_confirmation(self):
        service = self.service()
        answer = service.process("Который час?", self.reject_approval, CancellationToken())
        self.assertIn("Сейчас", answer)
        self.assertEqual([], self.model.calls)

    def test_local_declined_app_never_executes(self):
        tools = FakeToolRegistry()
        service = self.service(tools=tools)
        approvals = []

        def decline(call):
            approvals.append(call)
            return False

        answer = service.process("Открой блокнот", decline, CancellationToken())
        self.assertEqual("Действие отменено.", answer)
        self.assertEqual(1, len(approvals))
        self.assertEqual("open_app", approvals[0].name)
        self.assertEqual([], tools.executed)
        self.assertEqual([], self.model.calls)

    def test_tool_call_id_and_roles_preserved_for_followup(self):
        model = FakeModel(reply_with(ToolCall("get_time", "{}", "call-7")),
                          ModelReply("Готово, время узнал."))
        service = self.service(model)
        answer = service.process("Назови время через модель", self.reject_approval, CancellationToken())
        self.assertEqual("Готово, время узнал.", answer)
        self.assertEqual(2, len(model.calls))
        self.assertTrue(model.calls[0][1])
        messages = model.calls[1][0]
        call_message = next(m for m in messages if m.role == "assistant")
        tool_message = next(m for m in messages if m.role == "tool")
        self.assertEqual("call-7", call_message.tool_calls[0].id)
        self.assertEqual("call-7", tool_message.tool_call_id)
        self.assertIn("Сейчас", tool_message.content)

    def test_unknown_tool_rejected_before_confirmation(self):
        model = FakeModel(reply_with(ToolCall("run_powershell", '{"command":"shutdown"}', "evil")))
        service = self.service(model)
        with self.assertRaises(AssistantError):
            service.process("Выполни сложную задачу", self.reject_approval, CancellationToken())
        self.assertEqual(0, self.events.count("tool.executed"))

    def test_entire_batch_validated_before_any_effect(self):
        model = FakeModel(reply_with(ToolCall("get_time", "{}", "safe"),
                                    ToolCall("run_shell", '{"command":"shutdown"}', "evil")))
        service = self.service(model)
        with self.assertRaises(AssistantError):
            service.process("Выполни несколько задач", self.reject_approval, CancellationToken())
        self.assertEqual(0, self.events.count("tool.executed"))

    def test_max_tool_rounds_prevents_another_execution(self):
        settings = AppSettings(max_tool_rounds=1)
        model = FakeModel(reply_with(ToolCall("get_time", "{}", "time-1")),
                          reply_with(ToolCall("get_time", "{}", "time-2")))
        service = self.service(model, settings)
        with self.assertRaises(AssistantError):
            service.process("Сложный вопрос о времени", self.reject_approval, CancellationToken())
        self.assertEqual(2, len(model.calls))
        self.assertEqual(1, self.events.count("tool.executed"))

    def test_precancelled_request_never_calls_model(self):
        token = CancellationToken()
        token.cancel()
        service = self.service()
        with self.assertRaises(CancelledError):
            service.process("Запрос к модели", self.reject_approval, token)
        self.assertEqual([], self.model.calls)

    def test_truncated_tool_reply_executes_nothing(self):
        service = self.service(FakeModel(ModelReply("Частичный ответ",
                                                  (ToolCall("get_time", "{}", "time"),), "length")))
        with self.assertRaises(AssistantError):
            service.process("Сложный вопрос", self.reject_approval, CancellationToken())
        self.assertEqual(0, self.events.count("tool.executed"))
        self.assertEqual((), tuple(service.get_history_snapshot()))

    def test_missing_or_duplicate_call_ids_reject_entire_batch(self):
        for calls in ((ToolCall("get_time", "{}", ""),),
                      (ToolCall("get_time", "{}", "same"), ToolCall("get_time", "{}", "same"))):
            with self.subTest(calls=calls):
                service = self.service(FakeModel(reply_with(*calls)))
                with self.assertRaises(AssistantError):
                    service.process("Сложный вопрос", self.reject_approval, CancellationToken())
                self.assertEqual(0, self.events.count("tool.executed"))

    def test_later_decline_prevents_every_action_in_batch(self):
        tools = FakeToolRegistry()
        service = self.service(FakeModel(reply_with(
            ToolCall("open_app", '{"app":"notepad"}', "app-1"),
            ToolCall("open_app", '{"app":"calculator"}', "app-2"))), tools=tools)
        approvals = []

        def approve_first(call):
            approvals.append(call)
            return "notepad" in call.arguments_json

        answer = service.process("Выполни несколько действий", approve_first, CancellationToken())
        self.assertEqual("Действие отменено.", answer)
        self.assertEqual(2, len(approvals))
        self.assertEqual([], tools.executed)
        self.assertEqual(0, self.events.count("tool.executed"))

    def test_canonical_duplicate_actions_rejected_before_approval(self):
        tools = FakeToolRegistry()
        service = self.service(FakeModel(reply_with(
            ToolCall("open_app", '{"app":"notepad"}', "app-1"),
            ToolCall("open_app", '{ "app" : "NOTEPAD" }', "app-2"))), tools=tools)
        with self.assertRaises(AssistantError):
            service.process("Выполни действие дважды", self.reject_approval, CancellationToken())
        self.assertEqual([], tools.executed)

    def test_provider_failure_reports_completed_action_once(self):
        tools = FakeToolRegistry()
        model = FakeModel(reply_with(ToolCall("open_app", '{"app":"notepad"}', "app")),
                          AssistantError("Groq test failure"))
        service = self.service(model, tools=tools)
        question = "Do an action then answer"
        answer = service.process(question, lambda call: True, CancellationToken())
        self.assertIn("Не удалось завершить запрос", answer)
        self.assertIn("Groq test failure", answer)
        self.assertEqual(1, answer.count(tools.result))
        self.assertEqual(1, len(tools.executed))
        self.assert_history(service, question, answer)

    def test_cancellation_after_action_reports_nonrollback(self):
        token = CancellationToken()
        tools = FakeToolRegistry(on_execute=token.cancel)
        model = FakeModel(reply_with(ToolCall("open_app", '{"app":"notepad"}', "app")))
        service = self.service(model, tools=tools)
        question = "Do an action then cancel"
        answer = service.process(question, lambda call: True, token)
        self.assertIn("Запрос отменён", answer)
        self.assertIn("не откатываются", answer)
        self.assertEqual(1, answer.count(tools.result))
        self.assertEqual(1, len(model.calls))
        self.assert_history(service, question, answer)

    def test_success_contains_deterministic_completed_action_summary(self):
        tools = FakeToolRegistry()
        model = FakeModel(reply_with(ToolCall("open_app", '{"app":"notepad"}', "app")),
                          ModelReply("Задача завершена."))
        service = self.service(model, tools=tools)
        question = "Do an action successfully"
        answer = service.process(question, lambda call: True, CancellationToken())
        self.assertTrue(answer.startswith("Задача завершена.\nУже выполнено:"))
        self.assertEqual(1, answer.count(tools.result))
        self.assert_history(service, question, answer)

    def test_bounded_history_retains_complete_pairs_and_clear(self):
        model = FakeModel(ModelReply("answer one"), ModelReply("answer two"), ModelReply("answer three"))
        service = self.service(model, AppSettings(history_messages=4))
        for question in ("question one", "question two", "question three"):
            service.process(question, self.reject_approval, CancellationToken())
        history = service.get_history_snapshot()
        self.assertEqual(["user", "assistant", "user", "assistant"], [m.role for m in history])
        self.assertEqual(["question two", "answer two", "question three", "answer three"],
                         [m.content for m in history])
        service.clear_history()
        self.assertEqual((), tuple(service.get_history_snapshot()))
        self.assertEqual(1, self.events.count("history.cleared"))

    def test_disabled_and_odd_history_sizes_keep_complete_pairs(self):
        for limit, expected_count in ((0, 0), (1, 0), (3, 2)):
            with self.subTest(limit=limit):
                service = self.service(FakeModel(ModelReply("one"), ModelReply("two")),
                                       AppSettings(history_messages=limit))
                service.process("question one", self.reject_approval, CancellationToken())
                service.process("question two", self.reject_approval, CancellationToken())
                self.assertEqual(expected_count, len(service.get_history_snapshot()))

    def test_empty_or_oversized_input_never_calls_model(self):
        for question in ("", "   ", "x" * 4001):
            with self.subTest(length=len(question)):
                service = self.service()
                with self.assertRaises(AssistantError):
                    service.process(question, self.reject_approval, CancellationToken())
                self.assertEqual([], self.model.calls)

    def test_cancel_during_confirmation_prevents_execution(self):
        token = CancellationToken()
        tools = FakeToolRegistry()
        service = self.service(tools=tools)

        def confirm(call):
            token.cancel()
            return True

        with self.assertRaises(CancelledError):
            service.process("Открой блокнот", confirm, token)
        self.assertEqual([], tools.executed)

    def test_repeated_action_in_later_round_never_executes_twice(self):
        tools = FakeToolRegistry()
        model = FakeModel(reply_with(ToolCall("open_app", '{"app":"notepad"}', "first")),
                          reply_with(ToolCall("open_app", '{"app":"NOTEPAD"}', "second")))
        service = self.service(model, tools=tools)
        approvals = []

        def approve(call):
            approvals.append(call)
            return True

        answer = service.process("Do an action", approve, CancellationToken())
        self.assertIn("повтор", answer.casefold())
        self.assertEqual(1, len(approvals))
        self.assertEqual(1, len(tools.executed))
        self.assertEqual(1, answer.count(tools.result))

    def test_reused_call_id_across_rounds_rejected_before_execution(self):
        model = FakeModel(reply_with(ToolCall("get_time", "{}", "same")),
                          reply_with(ToolCall("get_time", "{}", "same")))
        service = self.service(model)
        with self.assertRaises(AssistantError):
            service.process("Сложный вопрос", self.reject_approval, CancellationToken())
        self.assertEqual(1, self.events.count("tool.executed"))

    def test_oversized_tool_batch_executes_nothing(self):
        model = FakeModel(reply_with(*(ToolCall("get_time", "{}", f"time-{i}") for i in range(6))))
        service = self.service(model)
        with self.assertRaises(AssistantError):
            service.process("Сложный вопрос", self.reject_approval, CancellationToken())
        self.assertEqual(0, self.events.count("tool.executed"))

    def test_empty_final_model_answer_is_not_remembered(self):
        for content in (None, "", "   "):
            with self.subTest(content=content):
                service = self.service(FakeModel(ModelReply(content)))
                with self.assertRaises(AssistantError):
                    service.process("Сложный вопрос", self.reject_approval, CancellationToken())
                self.assertEqual((), service.get_history_snapshot())

    def test_failed_turn_releases_lock_and_does_not_pollute_history(self):
        model = FakeModel(AssistantError("Fake failure"), ModelReply("Recovered"))
        service = self.service(model)
        with self.assertRaises(AssistantError):
            service.process("Failed question", self.reject_approval, CancellationToken())
        self.assertEqual("Recovered", service.process("Next question", self.reject_approval, CancellationToken()))
        self.assert_history(service, "Next question", "Recovered")

    def test_unexpected_error_after_action_reports_effect_without_details(self):
        tools = FakeToolRegistry()
        model = FakeModel(reply_with(ToolCall("open_app", '{"app":"notepad"}', "app")),
                          RuntimeError("SECRET_PROVIDER_DETAILS"))
        service = self.service(model, tools=tools)
        answer = service.process("Do an action then fail", lambda call: True, CancellationToken())
        self.assertIn("Ошибка обработки запроса", answer)
        self.assertNotIn("SECRET", answer)
        self.assertEqual(1, answer.count(tools.result))
        self.assert_history(service, "Do an action then fail", answer)

    def test_concurrent_turns_preserve_serial_history(self):
        entered, release = Event(), Event()
        model = FakeModel(ModelReply("First answer"), ModelReply("Second answer"))
        complete = model.complete

        def blocking_complete(messages, schemas, token):
            reply = complete(messages, schemas, token)
            if len(model.calls) == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("First request was not released")
            return reply

        model.complete = blocking_complete
        service = self.service(model)
        first, first_outcome = self.start_request(service, "First question")
        second = None
        try:
            self.assertTrue(entered.wait(2))
            second, second_outcome = self.start_request(service, "Second question")
            self.assertEqual(1, len(model.calls))
            release.set()
            self.assertEqual("First answer", first_outcome.result(2))
            self.assertEqual("Second answer", second_outcome.result(2))
            self.assertEqual(["First question", "First answer", "Second question"],
                             [message.content for message in model.calls[1][0][1:]])
            self.assertEqual(["First question", "First answer", "Second question", "Second answer"],
                             [message.content for message in service.get_history_snapshot()])
        finally:
            release.set()
            first.join(2)
            if second is not None:
                second.join(2)

    def test_waiting_turn_can_cancel_without_calling_model(self):
        entered, release = Event(), Event()
        model = FakeModel(ModelReply("First answer"))
        complete = model.complete

        def blocking_complete(messages, schemas, token):
            reply = complete(messages, schemas, token)
            entered.set()
            if not release.wait(2):
                raise AssertionError("First request was not released")
            return reply

        model.complete = blocking_complete
        service = self.service(model)
        first, first_outcome = self.start_request(service, "First question")
        second = None
        try:
            self.assertTrue(entered.wait(2))
            token = CancellationToken()
            second, second_outcome = self.start_request(service, "Cancelled question", token)
            token.cancel()
            with self.assertRaises(CancelledError):
                second_outcome.result(2)
            release.set()
            self.assertEqual("First answer", first_outcome.result(2))
            self.assertEqual(1, len(model.calls))
            self.assert_history(service, "First question", "First answer")
        finally:
            release.set()
            first.join(2)
            if second is not None:
                second.join(2)


if __name__ == "__main__":
    unittest.main()
