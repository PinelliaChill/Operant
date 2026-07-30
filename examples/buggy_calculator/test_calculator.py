import unittest

from calculator import add, divide


class CalculatorTests(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_divide_preserves_fraction(self) -> None:
        self.assertEqual(divide(5, 2), 2.5)

    def test_divide_by_zero_has_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "divisor must not be zero"):
            divide(5, 0)


if __name__ == "__main__":
    unittest.main()
