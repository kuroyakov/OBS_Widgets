import unittest

from server.translator import (
    infer_foreign_lang,
    is_probably_japanese,
    translate_to_japanese,
)


class TestJapaneseDetectionHalfwidthKana(unittest.TestCase):
    def test_halfwidth_kana_only_is_japanese(self):
        self.assertTrue(is_probably_japanese("ｺﾝﾊﾞﾝﾜ"))
        self.assertTrue(is_probably_japanese("ｦｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ"))
        self.assertTrue(is_probably_japanese("ｶﾞﾝﾊﾞﾚ!"))

    def test_halfwidth_kana_mixed_with_fullwidth_is_japanese(self):
        self.assertTrue(is_probably_japanese("ｺﾝﾊﾞﾝﾜです"))
        self.assertTrue(is_probably_japanese("今日ｺﾝﾊﾞﾝﾜ"))
        self.assertTrue(is_probably_japanese("ナイスｺﾒﾝﾄ"))

    def test_english_only_is_not_japanese(self):
        self.assertFalse(is_probably_japanese("Hello world"))
        self.assertFalse(is_probably_japanese("good morning"))

    def test_kanji_only_is_not_japanese(self):
        self.assertFalse(is_probably_japanese("你好"))
        self.assertFalse(is_probably_japanese("感謝"))
        self.assertEqual(infer_foreign_lang("你好"), "zh-CN")
        self.assertEqual(infer_foreign_lang("感謝"), "zh-CN")

    def test_translate_to_japanese_skips_halfwidth_kana(self):
        result = translate_to_japanese("ｺﾝﾊﾞﾝﾜ")
        self.assertEqual(
            result,
            {
                "original_text": "ｺﾝﾊﾞﾝﾜ",
                "translated_text": "ｺﾝﾊﾞﾝﾜ",
                "detected_lang": "ja",
                "is_translated": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
