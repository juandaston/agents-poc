import unittest

from conversation_history import (
    format_history_block,
    is_likely_followup,
    normalize_messages,
    resolve_effective_question,
)


class ConversationHistoryTests(unittest.TestCase):
    def test_normalize_messages_caps_count(self):
        raw = [{"role": "user", "content": f"msg {i}"} for i in range(12)]
        out = normalize_messages(raw)
        self.assertEqual(len(out), 6)
        self.assertEqual(out[0]["content"], "msg 6")

    def test_normalize_messages_caps_and_filters(self):
        raw = [
            {"role": "user", "content": "  seguros junio  "},
            {"role": "assistant", "content": "Total -1132625"},
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": ""},
        ]
        out = normalize_messages(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "seguros junio")

    def test_followup_detection(self):
        self.assertTrue(is_likely_followup("y en mayo?"))
        self.assertTrue(is_likely_followup("lo mismo para 2025"))
        self.assertFalse(is_likely_followup("cuánto gastamos en seguros en junio 2026"))

    def test_resolve_effective_question(self):
        history = [
            {"role": "user", "content": "cuánto gastamos en seguros en junio 2026"},
            {"role": "assistant", "content": "1.132.625"},
        ]
        eff = resolve_effective_question("y en mayo?", history)
        self.assertIn("seguros", eff)
        self.assertIn("mayo", eff)

    def test_format_history_excludes_current_user(self):
        history = [
            {"role": "user", "content": "pregunta anterior"},
            {"role": "assistant", "content": "respuesta"},
            {"role": "user", "content": "y en mayo?"},
        ]
        block = format_history_block(history)
        self.assertIn("pregunta anterior", block)
        self.assertNotIn("Usuario: y en mayo?", block)


if __name__ == "__main__":
    unittest.main()
