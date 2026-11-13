from app.chat import tokens


def test_deepseek_tokenizer_matches_omp_fixtures() -> None:
    cases = [
        ("", 0),
        ("hello world", 2),
        ("The quick brown fox jumps over the lazy dog.", 10),
        ("东京は日本の首都であり、世界で最も人口の多い都市圏の一つです。", 19),
        ("naïve café résumé — em-dash…ellipsis", 13),
        ("👍🏽 emoji test 👨‍👩‍👧‍👦 family, flags 🇹🇷🇯🇵", 28),
        ("fn main() {\n    let x: Vec<u32> = (0..10).collect();\n}", 22),
        ("C++ -O2 --flag=value", 9),
        ("<｜begin▁of▁sentence｜>", 12),
    ]
    for text, expected in cases:
        assert tokens.count_deepseek(text) == expected, text


def test_non_deepseek_falls_back_without_claiming_exact() -> None:
    count, exact = tokens.count_tokens("hello world", "gpt-4o")
    assert count >= 1
    assert exact is False
