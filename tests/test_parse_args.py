"""Разбор аргументов /add и /sell: имя может состоять из нескольких слов."""
import ast, os, unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def load(name):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(compile(ast.Module([node], []), SRC, "exec"), ns)
            return ns[name]
    raise AssertionError("в bot.py нет функции " + name)


class ParseClientArgs(unittest.TestCase):
    def setUp(self):
        self.parse = load("parse_client_args")

    def test_one_word_name_unchanged(self):
        self.assertEqual(self.parse(["Иван", "5млн", "Советский", "3к"]), ("Иван", ["5млн", "Советский", "3к"]))

    def test_two_word_name(self):
        self.assertEqual(self.parse(["Сергей", "Бахметьев", "15млн", "Словцова_10", "65м2", "3к"]),
                         ("Сергей Бахметьев", ["15млн", "Словцова_10", "65м2", "3к"]))

    def test_three_word_name(self):
        self.assertEqual(self.parse(["Анна", "Петровна", "Иванова", "8млн"]), ("Анна Петровна Иванова", ["8млн"]))

    def test_name_only(self):
        self.assertEqual(self.parse(["Иван"]), ("Иван", []))
        self.assertEqual(self.parse(["Иван", "Петров"]), ("Иван Петров", []))

    def test_starts_with_number_means_no_name(self):
        self.assertEqual(self.parse(["5млн", "Советский"]), ("", ["5млн", "Советский"]))

    def test_empty(self):
        self.assertEqual(self.parse([]), ("", []))

    def test_extra_spaces_and_name_with_digit_inside_kept(self):
        self.assertEqual(self.parse(["Анна-2", "7млн"]), ("Анна-2", ["7млн"]))


if __name__ == "__main__":
    unittest.main()
