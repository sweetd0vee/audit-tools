import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FUNCTIONS = ROOT / "seed" / "openwebui" / "functions"
SEED_PIPE = ROOT / "seed" / "openwebui" / "seed_pipe.py"
INTENT_PATH = FUNCTIONS / "intent.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPipeClassify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.intent = _load(INTENT_PATH, "audit_intent")
        cls.Cmd = cls.intent.Cmd

    def cmd(self, text: str, *, has_case: bool = True) -> str:
        return self.intent.classify(text, has_case=has_case)

    def test_help_phrases(self):
        for phrase in ("помощь", "help", "/help", "?", "  "):
            self.assertEqual(self.cmd(phrase), self.Cmd.HELP, phrase)

    def test_ask_question_prefix(self):
        self.assertEqual(
            self.cmd("вопрос Какой срок возврата валютной выручки по аренде нерезиденту?"),
            self.Cmd.ASK,
        )
        self.assertEqual(
            self.cmd("вопрос Можно ли в договоре аренды с резидентом РБ устанавливать арендную плату в евро?"),
            self.Cmd.ASK,
        )
        self.assertEqual(self.cmd("вопрос"), self.Cmd.ASK)
        self.assertEqual(self.cmd("/ask срок регистрации"), self.Cmd.ASK)
        self.assertEqual(self.cmd("/вопрос статья 625 ГК"), self.Cmd.ASK)

    def test_ask_wins_over_brief_and_library(self):
        self.assertEqual(
            self.cmd("вопрос Какой срок … саммари по акту"),
            self.Cmd.ASK,
        )
        self.assertEqual(
            self.cmd("вопрос в данном документе какая статья"),
            self.Cmd.ASK,
        )
        self.assertEqual(self.cmd("вопрос статья 625 ГК?"), self.Cmd.ASK)

    def test_total_before_brief(self):
        self.assertEqual(self.cmd("саммари total"), self.Cmd.TOTAL)
        self.assertEqual(self.cmd("саммари тотал"), self.Cmd.TOTAL)
        self.assertEqual(self.cmd("конспект модели"), self.Cmd.TOTAL)
        self.assertEqual(self.cmd("саммари total заново"), self.Cmd.TOTAL)
        self.assertEqual(self.cmd("саммари"), self.Cmd.BRIEF)
        self.assertEqual(self.cmd("саммари заново"), self.Cmd.BRIEF)
        self.assertEqual(self.cmd("сводка"), self.Cmd.BRIEF)

    def test_select_hypotheses_before_approve_and_build(self):
        self.assertEqual(
            self.cmd("утверждаю гипотезы 1, 3, 5"),
            self.Cmd.SELECT_HYPOTHESES,
        )
        self.assertEqual(
            self.cmd("подтверждаю гипотезы 1, 3, 5"),
            self.Cmd.SELECT_HYPOTHESES,
        )
        self.assertEqual(
            self.cmd("утверждаю гипотезы 1 и аудиторское мнение"),
            self.Cmd.SELECT_HYPOTHESES,
        )
        self.assertEqual(
            self.cmd("утверждаю гипотезы все с приоритетом высокий"),
            self.Cmd.SELECT_HYPOTHESES,
        )
        self.assertEqual(self.cmd("утверждаю все гипотезы"), self.Cmd.SELECT_HYPOTHESES)
        self.assertEqual(self.cmd("гипотезы"), self.Cmd.HYPOTHESES)
        self.assertEqual(self.cmd("чеклист гипотез"), self.Cmd.HYPOTHESES)

    def test_approve_numbers_and_required(self):
        self.assertEqual(self.cmd("утверждаю 1, 2"), self.Cmd.APPROVE)
        self.assertEqual(self.cmd("утверждаю 1, 2, 4"), self.Cmd.APPROVE)
        self.assertEqual(self.cmd("утверждаю все обязательные"), self.Cmd.APPROVE)
        self.assertEqual(self.cmd("подтверждаю 1, 2"), self.Cmd.APPROVE)
        self.assertEqual(self.cmd("выбираю 1, 2, 4"), self.Cmd.APPROVE)

    def test_approve_url_and_extra_title(self):
        self.assertEqual(
            self.cmd("к 3 url https://pravo.by/document/123"),
            self.Cmd.APPROVE,
        )
        self.assertEqual(
            self.cmd("утверждаю 1, 2 плюс Инструкция НБРБ № 38"),
            self.Cmd.APPROVE,
        )
        self.assertEqual(
            self.cmd("добавь Инструкция о порядке проведения валютных операций"),
            self.Cmd.APPROVE,
        )

    def test_download_retry_only_with_case(self):
        self.assertEqual(self.cmd("скачай", has_case=True), self.Cmd.APPROVE)
        self.assertEqual(self.cmd("скачай ещё раз", has_case=True), self.Cmd.APPROVE)
        self.assertEqual(self.cmd("скачай", has_case=False), self.Cmd.CHAT)
        self.assertEqual(
            self.cmd("можно скачать акт с pravo.by?", has_case=True),
            self.Cmd.CHAT,
        )
        self.assertEqual(self.cmd("скачай саммари", has_case=True), self.Cmd.BRIEF)

    def test_reject_approve(self):
        self.assertEqual(self.cmd("не утверждаю", has_case=True), self.Cmd.CHAT)
        self.assertEqual(self.cmd("не утверждаю 1, 2", has_case=True), self.Cmd.CHAT)
        self.assertEqual(
            self.cmd("не утверждаю гипотезы 1, 3", has_case=True),
            self.Cmd.CHAT,
        )

    def test_opinion_conclusion_program(self):
        self.assertEqual(self.cmd("аудиторское мнение"), self.Cmd.OPINION)
        self.assertEqual(self.cmd("аудиторское мнение -c"), self.Cmd.OPINION)
        self.assertEqual(self.cmd("аудиторское заключение"), self.Cmd.CONCLUSION)
        self.assertEqual(self.cmd("программа проверки"), self.Cmd.PROGRAM)
        self.assertEqual(self.cmd("составь программу"), self.Cmd.PROGRAM)
        self.assertEqual(self.cmd("программа проверки 10-12"), self.Cmd.PROGRAM)
        self.assertEqual(self.cmd("программа проверки заново"), self.Cmd.PROGRAM)

    def test_library_and_status(self):
        self.assertEqual(self.cmd("документы"), self.Cmd.LIBRARY)
        self.assertEqual(self.cmd("покажи документы"), self.Cmd.LIBRARY)
        self.assertEqual(self.cmd("статус"), self.Cmd.STATUS)
        self.assertEqual(self.cmd("кейсы"), self.Cmd.STATUS)
        self.assertEqual(self.cmd("проверки"), self.Cmd.STATUS)

    def test_new_case_explicit_start_only(self):
        text = "Проверка аренды коммерческой недвижимости, аренда, НДС"
        self.assertEqual(self.cmd(text, has_case=False), self.Cmd.NEW_CASE)
        self.assertEqual(self.cmd(text, has_case=True), self.Cmd.CHAT)
        self.assertEqual(
            self.cmd("Новая проверка кассовых операций, касса", has_case=False),
            self.Cmd.NEW_CASE,
        )

    def test_false_new_case_negatives(self):
        for phrase in (
            "какие сроки регистрации?",
            "какие сроки?",
            "в данной проверке какие сроки?",
            "проверк",
            "аренда коммерческой недвижимости, аренда, НДС",
            "нужен аудит кассы и валюты",
        ):
            self.assertEqual(
                self.cmd(phrase, has_case=False),
                self.Cmd.CHAT,
                phrase,
            )

    def test_bare_chat_with_case(self):
        self.assertEqual(
            self.cmd("какие сроки регистрации?"),
            self.Cmd.CHAT,
        )
        self.assertEqual(self.cmd("расскажи про аренду"), self.Cmd.CHAT)

    def test_program_items_spec(self):
        parse = self.intent._parse_program_items_spec
        self.assertEqual(parse("программа проверки 10-12"), (10, 12))
        self.assertEqual(parse("программа проверки 8"), (8, 8))
        self.assertIsNone(parse("программа проверки"))

    def test_parse_new_case_splits_keywords(self):
        parsed = self.intent._parse_new_case(
            "Проверка аренды коммерческой недвижимости, аренда, валюта, НДС"
        )
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["inspection_name"].startswith("Проверка аренды"))
        self.assertEqual(parsed["keywords"], ["аренда", "валюта", "НДС"])
        self.assertIsNone(self.intent._parse_new_case("аренда, НДС, валюта"))
        self.assertIsNone(self.intent._parse_new_case("в данной проверке какие сроки?"))

    def test_parse_kb_question(self):
        parse = self.intent._parse_kb_question
        self.assertEqual(parse("вопрос Какой срок?"), "Какой срок?")
        self.assertEqual(parse("/ask срок"), "срок")
        self.assertIsNone(parse("Какой срок?"))

    def test_hypothesis_picks(self):
        picks = self.intent._parse_hypothesis_picks
        self.assertEqual(
            picks("утверждаю гипотезы 1, 3, 5")["numbers"],
            [1, 3, 5],
        )
        self.assertTrue(picks("утверждаю все гипотезы")["all_rows"])
        self.assertTrue(
            picks("утверждаю гипотезы все с приоритетом высокий")["all_high"]
        )

    def test_resolve_approval_numbers(self):
        docs = [
            {"id": "aa1111111111", "priority": 1},
            {"id": "bb2222222222", "priority": 2},
            {"id": "cc3333333333", "priority": 1},
        ]
        ids, manuals, extras = self.intent._resolve_approval("утверждаю 1, 2", docs)
        self.assertEqual(ids, ["aa1111111111", "bb2222222222"])
        self.assertEqual(manuals, {})
        self.assertEqual(extras, [])
        ids, _, _ = self.intent._resolve_approval("утверждаю все обязательные", docs)
        self.assertEqual(ids, ["aa1111111111", "cc3333333333"])


class TestPipePaste(unittest.TestCase):
    def test_concatenated_paste_compiles_without_intent_import(self):
        seed = _load(SEED_PIPE, "seed_pipe")
        source = seed.build_pipe_source()
        compile(source, "audit_agent.paste.py", "exec")
        self.assertIn("class Pipe:", source)
        self.assertIn("def classify(", source)
        self.assertIn("class Cmd:", source)
        self.assertNotIn("from intent import", source)
        self.assertIn("NEW_CASE_START_RE", source)
        self.assertIn("def _format_elapsed(", source)
        self.assertIn("Сгенерировано за", source)


class TestPipeElapsed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = _load(FUNCTIONS / "audit_agent.py", "audit_agent")

    def test_format_elapsed(self):
        fmt = self.agent._format_elapsed
        self.assertEqual(fmt(None), "")
        self.assertEqual(fmt("x"), "")
        self.assertEqual(fmt(0), "")
        self.assertEqual(fmt(400), "")
        self.assertEqual(fmt(1000), "1 с")
        self.assertEqual(fmt(61000), "1 мин 1 с")
        self.assertEqual(fmt(3723000), "1 ч 2 мин 3 с")

    def test_status_and_footer(self):
        with_elapsed = self.agent._with_elapsed
        footer = self.agent._elapsed_footer
        self.assertEqual(with_elapsed("пишу", 400), "пишу")
        self.assertEqual(with_elapsed("пишу", 61000), "пишу · 1 мин 1 с")
        self.assertEqual(
            footer({"elapsed_ms": 125000}),
            "Сгенерировано за 2 мин 5 с.",
        )
        self.assertEqual(
            footer({"reused": True, "built_elapsed_ms": 125000, "elapsed_ms": 9}),
            "Файл уже был готов. В прошлый раз генерация заняла 2 мин 5 с.",
        )
        self.assertEqual(footer({"reused": True, "elapsed_ms": 9}), "Файл уже был готов.")


if __name__ == "__main__":
    unittest.main()
