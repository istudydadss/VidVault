import unittest

from app.config import settings
from app.routers import chat


class ChatRoutingTest(unittest.TestCase):
    def test_short_acknowledgement_is_general_question(self):
        for question in ["好的", "OK", "收到，谢谢"]:
            with self.subTest(question=question):
                self.assertTrue(chat._is_general_question(question))

    def test_general_question_stays_direct_even_with_collection_context(self):
        self.assertEqual(
            chat._route_with_rules("好的", is_collection_intent=True, related=True),
            "direct",
        )

    def test_llm_router_is_disabled_by_default(self):
        self.assertFalse(settings.chat_use_llm_router)


if __name__ == "__main__":
    unittest.main()
